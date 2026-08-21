import os
import openai
from typing import Dict, Any, Tuple
import math
from collections import Counter
import difflib

# Utility for naive memory similarity
def _cosine_similarity(text1: str, text2: str) -> float:
    words1 = text1.lower().split()
    words2 = text2.lower().split()
    vec1 = Counter(words1)
    vec2 = Counter(words2)
    intersection = set(vec1.keys()) & set(vec2.keys())
    numerator = sum([vec1[x] * vec2[x] for x in intersection])
    sum1 = sum([vec1[x]**2 for x in vec1.keys()])
    sum2 = sum([vec2[x]**2 for x in vec2.keys()])
    denominator = math.sqrt(sum1) * math.sqrt(sum2)
    return float(numerator) / denominator if denominator else 0.0

class EvaluationArm:
    """Base class for an evaluation arm in the benchmark."""
    def __init__(self, arm_name: str, model: str = "openai/gpt-4o-mini"):
        self.arm_name = arm_name
        self.model = model
        self.client = openai.OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.environ.get("GROQ_API_KEY", "mock_key_for_tests")
        )
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        
    def reset_run(self):
        """Called at the start of a run (which contains 20 episodes). Clear memory."""
        pass

    def run_episode(self, env, task: Dict[str, Any], max_turns: int = 5) -> Dict[str, Any]:
        """Runs the agent on the current task until success or max_turns."""
        prompt = env.reset(task)
        
        # Format the memory context based on the arm configuration
        prompt = self._inject_memory_context(prompt, task)
        
        system_msg = "You are an autonomous agent fixing bugs. Output your fix in the requested markdown block."
        
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt}
        ]
        
        first_action_accuracy = False
        success = False
        retries = 0
        past_failed_actions_in_episode = []
        repeated_failure_rate = 0.0
        
        for turn in range(max_turns):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.0
                )
                action = response.choices[0].message.content
                
                # Token tracking
                if response.usage:
                    self.total_prompt_tokens += response.usage.prompt_tokens
                    self.total_completion_tokens += response.usage.completion_tokens
            except Exception as e:
                import traceback
                print(f"LLM API Error: {e}")
                traceback.print_exc()
                # Mock fallback if no API key
                action = "```python\n# fallback\n```" if "software" in task["domain"] else "```yaml\nversion: '3.8'\nservices:\n```"

            obs, is_success, is_failure, info = env.step(action)
            
            if turn == 0 and is_success:
                first_action_accuracy = True
                
            if not is_success:
                # Check for repeated failure using Action Edit Distance (AED)
                for past_action in past_failed_actions_in_episode:
                    ratio = difflib.SequenceMatcher(None, action, past_action).ratio()
                    if ratio > 0.85:
                        repeated_failure_rate = 1.0 # High semantic similarity to past failure
                        break
                past_failed_actions_in_episode.append(action)
                
            messages.append({"role": "assistant", "content": action})
            messages.append({"role": "user", "content": f"Observation: {obs}\nPlease try again."})
            
            if is_success:
                success = True
                break
                
            retries += 1

        # Calculate cost (rough estimate for gpt-4o-mini)
        cost_usd = (self.total_prompt_tokens / 1_000_000) * 0.15 + (self.total_completion_tokens / 1_000_000) * 0.60
        
        # After episode ends, store outcome in memory
        self._store_outcome(task, action, success, obs)
        
        # Reset counters for next episode return
        tp = self.total_prompt_tokens
        tc = self.total_completion_tokens
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        
        return {
            "success": success,
            "retries": retries,
            "first_action_accuracy": first_action_accuracy,
            "repeated_failure_rate": repeated_failure_rate,
            "tokens_prompt": tp,
            "tokens_completion": tc,
            "cost_usd": cost_usd
        }

    def _inject_memory_context(self, prompt: str, task: Dict[str, Any]) -> str:
        return prompt

    def _store_outcome(self, task: Dict[str, Any], final_action: str, success: bool, final_obs: str):
        pass

# ---------------------------------------------------------
# Arm 1: No Memory
# ---------------------------------------------------------
class ArmNoMemory(EvaluationArm):
    def __init__(self, **kwargs):
        super().__init__("Arm_1_NoMemory", **kwargs)

# ---------------------------------------------------------
# Arm 2: Naive Memory
# ---------------------------------------------------------
class ArmNaiveMemory(EvaluationArm):
    def __init__(self, **kwargs):
        super().__init__("Arm_2_NaiveMemory", **kwargs)
        self.memory_bank = []

    def reset_run(self):
        self.memory_bank = []

    def _inject_memory_context(self, prompt: str, task: Dict[str, Any]) -> str:
        if not self.memory_bank:
            return prompt
            
        # Retrieve top 3
        query = task.get("description", "") + " " + task.get("error_type", "")
        scored = [(m, _cosine_similarity(query, m['task_query'])) for m in self.memory_bank]
        scored.sort(key=lambda x: x[1], reverse=True)
        top_k = [x[0] for x in scored[:3] if x[1] > 0.0]
        
        if top_k:
            mem_str = "\n## Past Relevant Experiences:\n"
            for i, m in enumerate(top_k, 1):
                outcome = "SUCCESS" if m["success"] else "FAILURE"
                mem_str += f"{i}. Outcome: {outcome}\nAction Taken:\n{m['action'][:200]}...\nObservation: {m['obs'][:100]}\n"
            return mem_str + "\n" + prompt
        return prompt

    def _store_outcome(self, task: Dict[str, Any], final_action: str, success: bool, final_obs: str):
        query = task.get("description", "") + " " + task.get("error_type", "")
        self.memory_bank.append({
            "task_query": query,
            "action": final_action,
            "success": success,
            "obs": final_obs
        })

# ---------------------------------------------------------
# Arm 3 & 4: CogniCore (Retrieval Only & Full)
# ---------------------------------------------------------
from cognicore.runtime import CogniCoreRuntime, RuntimeConfig

class ArmCogniCore(EvaluationArm):
    def __init__(self, arm_name: str, use_reflection: bool, **kwargs):
        super().__init__(arm_name, **kwargs)
        self.use_reflection = use_reflection
        self.runtime = None

    def reset_run(self):
        # Fresh runtime per run
        self.runtime = CogniCoreRuntime(
            config=RuntimeConfig(reflection_min_samples=1, reflection_failure_threshold=1, memory_top_k=3),
            name=f"{self.arm_name}_run"
        )

    def _inject_memory_context(self, prompt: str, task: Dict[str, Any]) -> str:
        category = task.get("domain", "general")
        
        # 1. Semantic Recall (from episodic entries in the runtime's semantic wrapper, or just standard retrieval)
        mem_ctx = self.runtime._build_context(category)
        
        mem_str = ""
        if mem_ctx.get("memory"):
            mem_str += "\n## CogniCore Retrieved Experiences:\n"
            for e in mem_ctx["memory"][-3:]:
                out = "SUCCESS" if e.get("correct") else "FAILURE"
                mem_str += f"- [{out}] {e.get('predicted', '')}\n"
                
        if self.use_reflection and mem_ctx.get("reflection_hint"):
            mem_str += f"\n## CogniCore Reflection Hint:\n{mem_ctx['reflection_hint']}\n"
            
        if mem_str:
            return mem_str + "\n" + prompt
        return prompt

    def _store_outcome(self, task: Dict[str, Any], final_action: str, success: bool, final_obs: str):
        category = task.get("domain", "general")
        # Truncate action for memory footprint
        pred = f"Used code: {final_action[:200]}... Obs: {final_obs[:100]}"
        self.runtime.memory.store({
            "category": category,
            "correct": success,
            "predicted": pred
        })
        
        # Force reflection evaluation synchronously so it's ready for the next episode
        if self.use_reflection:
            query = task.get("description", "")
            retrieved = self.runtime.memory.retrieve(category)
            self.runtime.reflection.analyze(retrieved)

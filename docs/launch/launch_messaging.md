# CogniCore/NEXUS — Launch Messaging

All launch copy in one place. Every claim is backed by benchmarks from the ablation study (20 SWE-bench-lite tasks, 4 routing policies, 280+ trajectories).

---

## Hacker News — Show HN

**Title:** Show HN: CogniCore – Runtime cognition layer for AI agents (memory, replay, reflection)

**Body:**

Hi HN,

I built CogniCore, an open-source framework that adds persistent memory, reflection, and replay to AI agent runtimes. The core idea: instead of making models smarter, make the runtime smarter.

**The problem:** Every agent framework I've used treats each run as stateless. The agent tries something, it fails, the run ends, and the next run has zero memory of what happened. You pay the same token cost to make the same mistakes.

**What CogniCore does:**

- **Persistent memory** — Episodic, semantic, and cognitive memory systems (SQLite-backed). The agent accumulates experience across episodes and sessions. In our benchmarks, this takes solve rate from 38% to 95% on repeated episodes with the same model and prompt.

- **Replay and time travel** — Every agent decision is stored as an immutable event. You can replay any past run step-by-step, branch from any decision point, and compare outcomes. Think `git log` for agent cognition.

- **Reflection engine** — Analyzes failure patterns and generates insights that feed into future episodes. Not prompt-engineering — structured runtime learning.

- **Agent immune system** — Detects prompt injection, jailbreaks, resource attacks. RL-trained defender that improves over time.

**The counterintuitive finding:** We ran a full ablation study across 4 routing policies (minimal, test_first, standard, review_first) on 20 SWE-bench-lite tasks. Adding a code reviewer agent *reduces* solve rate from 95% to 90% while increasing token cost by 35% (extra 9,642 tokens). The reviewer's similarity-based rejection blocks valid patches that happen to be structurally similar to the buggy code. The minimal pipeline (just Coder + Tester) achieves 95% at 1,446 tokens per solve.

More agents ≠ better. Runtime cognition > agent count.

**Tech details:**
- 60+ Gymnasium-compatible environments (CodeDebugging, SafetyClassification, MathReasoning, Planning)
- Works with any LLM (OpenAI, Gemini, Claude, Ollama, HuggingFace, OpenRouter)
- 470+ tests passing
- Full trajectory store for offline RL training on coordination policies
- Apache 2.0 license

Install: `pip install cognicore-env`

GitHub: https://github.com/Kaushalt2004/cognicore-my-openenv

The benchmarks are reproducible — `python benchmarks/run_benchmarks.py`. The ablation paper is in the repo at `docs/paper/nexus_paper.tex`.

Happy to answer questions about the architecture, benchmark methodology, or the reviewer finding.

---

## Reddit — r/MachineLearning

**Title:** [P] CogniCore: Adding persistent memory to AI agent runtimes improves solve rate from 38% to 95% — and adding more agents actually hurts

**Body:**

I've been working on CogniCore, an open-source cognitive runtime layer for AI agents. The research question driving this: **does runtime cognition (memory, reflection, replay) matter more than multi-agent architecture (more specialized agents)?**

### The setup

CogniCore wraps any LLM-backed agent with:
- Persistent episodic memory (SQLite-backed, cross-session)
- A reflection engine that learns from failure patterns
- Event-sourced replay with time-travel branching
- An RL-trained immune system for input safety

The framework is Gymnasium-compatible and ships with 60+ environments across CodeDebugging, SafetyClassification, MathReasoning, and Planning.

### Key results

**Memory ablation:** On SafetyClassification across 20 episodes with 5 seeds, enabling persistent memory and reflection improves average solve rate from 38% to 95%. Same model, same prompt, same environment. The only variable is whether the runtime retains and reflects on experience.

**Multi-agent ablation:** We tested 4 routing policies on 20 SWE-bench-lite tasks:

| Policy | Solved | Tokens | Tokens/Solve |
|---|---|---|---|
| test_first (M→P→C→T) | 19/20 (95%) | 32,854 | 1,729 |
| minimal (C→T) | 19/20 (95%) | 27,476 | 1,446 |
| standard (M→P→C→R→T) | 18/20 (90%) | 37,118 | 2,062 |
| review_first (M→P→C→R→C→T) | 18/20 (90%) | 45,591 | 2,533 |

Adding a reviewer agent drops solve rate by 5% and adds 35% token cost. The reviewer uses similarity-based patch rejection and blocks valid fixes that are structurally close to the buggy code — a known limitation when patches are minor modifications rather than rewrites.

**Long-session stability:** Memory accumulates ~490 tokens per session with zero performance degradation or strategy drift across 3+ consecutive sessions.

### What this suggests

The standard assumption in multi-agent systems — that adding specialized agents improves outcomes — doesn't hold in our experiments. The optimal "cognition-per-token" ratio comes from minimal agent pipelines with persistent runtime state.

This aligns with a broader pattern: the bottleneck in agent performance is often not the model's capability, but the runtime's inability to retain and leverage experience.

### Links

- GitHub: https://github.com/Kaushalt2004/cognicore-my-openenv
- Install: `pip install cognicore-env`
- Paper: `docs/paper/nexus_paper.tex` in the repo
- 280+ recorded trajectories for offline RL research

Would be interested in hearing from anyone doing related work on agent memory systems or multi-agent ablation studies. The trajectory store is designed for offline RL — if anyone wants to train coordination policies on the data, it's all there.

---

## Twitter/X — Launch Thread

### Tweet 1 (Hook)

Your AI agent just made the same mistake for the 47th time.

Because it has zero memory of the last 46 attempts.

We built CogniCore — a runtime cognition layer that gives agents persistent memory, reflection, and replay.

Same model. Smarter runtime.

🧵 ↓

---

### Tweet 2 (The Problem)

Every agent framework treats every run as stateless.

Agent tries → fails → run ends → next run starts from zero.

Same tokens. Same mistakes. Same cost.

The model isn't the problem. The runtime is.

---

### Tweet 3 (The Result)

We benchmarked it.

Without memory: 38% solve rate
With CogniCore memory + reflection: 95% solve rate

Same model. Same prompt. Same environment.

The only change: the runtime retains experience and reflects on failures.

---

### Tweet 4 (The Counterintuitive Finding)

The part nobody expects:

Adding a code reviewer agent REDUCES solve rate by 5% and INCREASES token cost by 35%.

Full ablation — 4 policies, 20 SWE-bench tasks, 280+ trajectories.

More agents ≠ better performance.

Minimal pipeline (Coder + Tester) = 95% at 1,446 tokens/solve.

---

### Tweet 5 (What's Under the Hood)

What CogniCore actually ships:

🧠 Persistent memory (episodic, semantic, cognitive)
🔁 Event-sourced replay with time-travel branching
🛡️ RL-trained immune system (prompt injection, jailbreaks)
📊 60+ Gymnasium environments
🔌 Any LLM: OpenAI, Gemini, Claude, Ollama

470+ tests. Apache 2.0 license.

---

### Tweet 6 (Install)

```
pip install cognicore-env
```

5 lines of Python:

```python
import cognicore as cc
config = cc.CogniCoreConfig(enable_memory=True, enable_reflection=True)
env = cc.make("SafetyClassification-v1", config=config)
agent = AutoLearner()
# agent.learn(reward, info) — that's it
```

---

### Tweet 7 (CTA)

Benchmarks are reproducible. Paper is in the repo. Trajectory data is open for offline RL research.

⭐ github.com/Kaushalt2004/cognicore-my-openenv

If you're building agents that need to remember, try it.

If you find a case where more agents actually helps — I want to see the data.

---

## LinkedIn — Launch Post

**I spent the last few months building something I couldn't find anywhere else: a cognitive runtime for AI agents.**

Here's the problem I kept hitting: every AI agent framework treats each execution as stateless. The agent tries a solution, fails, and the run ends. The next run starts from zero — no memory of what was attempted, no learning from what failed. You pay the same token cost to make the same mistakes repeatedly.

**So I built CogniCore** — an open-source framework that adds persistent memory, reflection, and replay to any LLM-backed agent runtime.

**The results from our benchmarks:**

→ Persistent memory improves solve rate from 38% to 95% across repeated episodes (same model, same prompt — only the runtime changes)

→ Adding a code reviewer agent actually *reduces* solve rate by 5% while increasing token cost by 35% — tested across 4 routing policies, 20 SWE-bench-lite tasks, and 280+ recorded trajectories

→ Memory accumulates cross-session without performance degradation

→ The optimal pipeline uses just 1,446 tokens per solved task

**The counterintuitive takeaway:** more agents doesn't mean better performance. Runtime cognition — giving agents the ability to remember, reflect, and replay — matters more than adding specialized agents to the pipeline.

**What's in the box:**
• Persistent memory systems (episodic, semantic, cognitive — SQLite-backed)
• Event-sourced replay with time-travel branching
• Reflection engine that learns from failure patterns
• RL-trained agent immune system
• 60+ Gymnasium-compatible environments
• Works with any LLM provider (OpenAI, Gemini, Claude, Ollama, HuggingFace)
• 470+ tests passing
• Apache 2.0 license

The core message: **the model stays the same — the runtime gets smarter.**

Install: `pip install cognicore-env`
GitHub: https://github.com/Kaushalt2004/cognicore-my-openenv

The benchmarks are fully reproducible, and the ablation paper is included in the repository. If you're building AI agents that need to learn from experience rather than start from scratch every time, I'd appreciate a look and any feedback.

#AI #MachineLearning #OpenSource #AIAgents #LLM

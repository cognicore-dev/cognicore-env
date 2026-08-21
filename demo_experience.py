import sys
import logging
from typing import List, Optional, Dict

sys.stdout.reconfigure(encoding='utf-8')

from cognicore.experience.schema import StructuredExperience, EvidenceRecord, Attempt, AttemptOutcome, VerificationStatus, EnvironmentContext
from cognicore.experience.extractor import ExperienceExtractor
from cognicore.experience.verification import VerificationGate
from cognicore.experience.retrieval import ExperienceRetriever
from cognicore.memory.base import MemoryBackend, MemoryEntry, SearchResult

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("demo")

# A simple in-memory backend for demonstration
class InMemoryBackend(MemoryBackend):
    def __init__(self):
        self.entries = []
        self._next_id = 1

    def store(self, entry: MemoryEntry) -> str:
        entry.entry_id = str(self._next_id)
        self._next_id += 1
        self.entries.append(entry)
        return entry.entry_id

    def search(self, query: str, top_k: int = 5, **kwargs) -> List[SearchResult]:
        # Dumb text search for demo
        results = []
        query_words = set(query.lower().split())
        for e in self.entries:
            text_lower = e.text.lower()
            cat_lower = (e.category or "").lower()
            if any(w in text_lower or w in cat_lower for w in query_words if len(w) > 2):
                results.append(SearchResult(entry=e, score=0.9))
        return results[:top_k]

    def get_by_category(self, category: str, **kwargs) -> List[MemoryEntry]:
        return [e for e in self.entries if e.category == category]

    def count(self) -> int:
        return len(self.entries)

    def clear(self) -> None:
        self.entries.clear()

    def update(self, entry_id: str, **fields) -> bool:
        for e in self.entries:
            if e.entry_id == entry_id:
                for k, v in fields.items():
                    setattr(e, k, v)
                return True
        return False
        
    def delete(self, entry_id: str) -> bool:
        initial_len = len(self.entries)
        self.entries = [e for e in self.entries if e.entry_id != entry_id]
        return len(self.entries) < initial_len

def run_demo():
    print("\n" + "="*60)
    print(" CogniCore Structured Experience Demo: The Killer Version ")
    print("="*60)

    backend = InMemoryBackend()
    extractor = ExperienceExtractor()
    gate = VerificationGate()
    retriever = ExperienceRetriever()

    # ---------------------------------------------------------
    # SESSION 1: Agent A solves a problem
    # ---------------------------------------------------------
    print("\n[ SESSION 1 ]")
    print("Agent A faces task: 'fix divide by zero bug in math_utils.py'")
    
    agent_a_session_data = {
        "task": "fix divide by zero bug in math_utils.py",
        "problem": "ZeroDivisionError occurs when calling divide(a, 0)",
        "attempts": [
            {
                "approach": "Approach A: return 0 when b is 0",
                "outcome": "failure",
                "reason": "Tests failed: expected exception but got 0",
            },
            {
                "approach": "Approach B: print warning and return None",
                "outcome": "failure",
                "reason": "Tests failed: expected ValueError to be raised",
            },
            {
                "approach": "Approach C: raise ValueError('Cannot divide by zero')",
                "outcome": "success",
                "reason": "Tests passed successfully",
            }
        ],
        "solution": "Added `if b == 0: raise ValueError(...)` before division.",
        "why_it_worked": "Matches expected API contract tested by the test suite.",
        "source_agent": "Agent A",
        "source_session": "sess_001",
        "environment": {"python_version": "3.11", "os": "linux", "dependencies": {"math-lib": "1.0.0"}},
        "repository": {"repo_id": "math-lib", "commit": "abcdef12"}
    }
    
    experience = extractor.extract(agent_a_session_data)
    print("  ├── Approach A ❌")
    print("  ├── Approach B ❌")
    print("  └── Approach C ✅")
    print("             ↓")
    print("       47 tests pass")
    print("             ↓")
    
    evidence = [
        EvidenceRecord(command="pytest tests/", exit_code=0, stdout_hash="abc123hash", timestamp="2026-08-21T10:00:00Z", commit="abcdef12")
    ]
    gate.promote(experience, evidence, backend)
    
    if experience.verification_status == VerificationStatus.VERIFIED.value:
        print("       VERIFIED EXPERIENCE")
        
    for entry in experience.to_failure_entries():
        backend.store(entry)


    # ---------------------------------------------------------
    # SESSION 2: Fresh Agent B (No conversation history)
    # ---------------------------------------------------------
    print("\n[ SESSION 2 ]")
    print("Fresh Agent B")
    print("NO conversation history")
    print("             ↓")
    print("     Similar problem: 'fix divide by zero in calculate_ratio'")
    print("             ↓")
    
    # Target environment is the same
    current_env = EnvironmentContext(python_version="3.11", os="linux", dependencies={"math-lib": "1.0.2"})
    
    retrieval_result = retriever.retrieve(
        query="divide by zero math",
        backend=backend,
        current_env=current_env,
        include_failures=True,
        require_verified=True
    )
    
    print("     CogniCore retrieves")
    
    for fail in retrieval_result.failures:
        for attempt in fail.attempts:
            if attempt.outcome == 'failure':
                if "Approach A" in attempt.approach:
                    print("       ├── A → DON'T USE (Reason: {})".format(attempt.reason.split(':')[1].strip()))
                if "Approach B" in attempt.approach:
                    print("       ├── B → DON'T USE (Reason: {})".format(attempt.reason.split(':')[1].strip()))

    for exp in retrieval_result.experiences:
        print(f"       └── C → VERIFIED  (Solution: {exp.solution})")

    print("             ↓")
    print("          Agent B")
    print("             ↓")
    print("          SUCCESS")


    # ---------------------------------------------------------
    # SESSION 3: Dependency Changes
    # ---------------------------------------------------------
    print("\n[ SESSION 3 - ENVIRONMENT CHANGE ]")
    print("Dependency math-lib updates from v1 to v2.")
    print("        ↓")
    
    # Target environment changes major dependency version
    changed_env = EnvironmentContext(python_version="3.11", os="linux", dependencies={"math-lib": "2.0.0"})
    
    retrieval_result_stale = retriever.retrieve(
        query="divide by zero math",
        backend=backend,
        current_env=changed_env,
        include_failures=True,
        require_verified=True
    )
    
    print("Experience becomes stale")
    print("        ↓")
    
    if len(retrieval_result_stale.experiences) == 0:
        print("CogniCore refuses automatic trusted transfer")
        print(f"(Filtered out {retrieval_result_stale.filtered_out} incompatible candidates)")
        print("        ↓")
        print("Re-validation required")
    else:
        print("ERROR: CogniCore transferred a stale experience!")

    print("\n" + "="*60)

if __name__ == '__main__':
    run_demo()

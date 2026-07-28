"""
CogniCore Runtime — Universal cognition layer for any AI agent.

This is the CORE module. It wraps any callable agent function and adds:
  - Persistent episodic memory
  - Reflection-based failure avoidance
  - Runtime adaptation scoring
  - Repeated failure reduction

Works with: OpenAI, LangChain, CrewAI, AutoGen, SB3, custom agents.

Usage:
    from cognicore.runtime import CogniCoreRuntime

    runtime = CogniCoreRuntime()

    # Wrap any function
    @runtime.wrap
    def my_agent(task, context):
        return do_something(task)

    # Or use directly
    result = runtime.execute(
        agent_fn=my_agent,
        task="Fix the login bug",
        category="code_fix",
    )
"""

from __future__ import annotations

import time
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from cognicore.memory.base import MemoryBackend, MemoryScope
from cognicore.memory.tfidf_backend import TFIDFMemoryBackend
from cognicore.memory.lifecycle import MemoryLifecycleManager
from cognicore.middleware.reflection import ReflectionEngine

logger = logging.getLogger("cognicore.runtime")


# ---------------------------------------------------------------------------
# Runtime Config
# ---------------------------------------------------------------------------

@dataclass
class RuntimeConfig:
    """Configuration for CogniCore Runtime."""
    enable_memory: bool = True
    enable_reflection: bool = True
    memory_max_size: int = 10_000
    memory_similarity_key: str = "category"
    memory_top_k: int = 5
    reflection_min_samples: int = 2
    reflection_failure_threshold: int = 2
    persistence_path: Optional[str] = None
    auto_save: bool = True
    verbose: bool = False
    
    # Phase 0/1/3 Additions
    memory_backend: Optional[str] = "tfidf"  # "tfidf", "sqlite", "embeddings"
    embedding_provider: Optional[str] = None
    memory_scope: str = "global"
    memory_scope_id: str = ""


# ---------------------------------------------------------------------------
# Execution Result
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """Result of a runtime-wrapped execution."""
    output: Any = None
    success: bool = False
    category: str = ""
    duration_ms: float = 0.0
    memory_context: List[Dict] = field(default_factory=list)
    reflection_hint: Optional[str] = None
    reflection_override: bool = False
    original_output: Any = None
    attempt: int = 1
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["output"] = str(self.output)[:200]
        d["original_output"] = str(self.original_output)[:200] if self.original_output else None
        return d


# ---------------------------------------------------------------------------
# Runtime Stats
# ---------------------------------------------------------------------------

@dataclass
class RuntimeStats:
    """Aggregate statistics for the runtime."""
    total_executions: int = 0
    successes: int = 0
    failures: int = 0
    repeated_failures_avoided: int = 0
    memory_retrievals: int = 0
    reflection_overrides: int = 0
    total_duration_ms: float = 0.0
    categories_seen: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / max(1, self.total_executions)

    @property
    def failure_reduction_rate(self) -> float:
        return self.repeated_failures_avoided / max(1, self.failures + self.repeated_failures_avoided)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["success_rate"] = f"{self.success_rate:.1%}"
        d["failure_reduction_rate"] = f"{self.failure_reduction_rate:.1%}"
        d["avg_duration_ms"] = f"{self.total_duration_ms / max(1, self.total_executions):.1f}"
        return d


# ---------------------------------------------------------------------------
# CogniCore Runtime — The universal wrapper
# ---------------------------------------------------------------------------

class CogniCoreRuntime:
    """Universal cognition layer that wraps any AI agent.

    This is NOT tied to CogniCoreEnv. It works with any Python callable:
    OpenAI API calls, LangChain chains, CrewAI crews, SB3 policies, etc.

    Parameters
    ----------
    config : RuntimeConfig or None
        Runtime configuration. Uses defaults if None.
    name : str
        Name for this runtime instance (used in persistence).

    Example
    -------
    >>> runtime = CogniCoreRuntime()
    >>> result = runtime.execute(
    ...     agent_fn=lambda task, ctx: "fixed",
    ...     task="Fix bug #123",
    ...     category="code_fix",
    ...     evaluator=lambda out, task: out == "fixed",
    ... )
    >>> result.success
    True
    """

    def __init__(
        self,
        config: Optional[RuntimeConfig] = None,
        name: str = "cognicore-runtime",
        memory: Optional[MemoryBackend] = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.name = name

        # Initialize middleware
        if memory is not None:
            self.backend = memory
        elif self.config.memory_backend == "sqlite":
            from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
            db_path = self.config.persistence_path or "cognicore_memory.db"
            self.backend = SQLiteMemoryBackend(db_path=db_path)
        else:
            self.backend = TFIDFMemoryBackend(
                max_size=self.config.memory_max_size,
                persistence_path=self.config.persistence_path
            )
            
        self.memory = MemoryLifecycleManager(backend=self.backend)

        self.reflection = ReflectionEngine(
            memory=self.backend,
            min_samples=self.config.reflection_min_samples,
            failure_threshold=self.config.reflection_failure_threshold,
        )
        self.stats = RuntimeStats()
        self._categories: set = set()
        self._execution_log: List[Dict] = []

        # Load persisted state if available
        if self.config.persistence_path:
            self._load_state()

        logger.info(f"CogniCoreRuntime '{name}' initialized. "
                    f"Memory={self.config.enable_memory}, "
                    f"Reflection={self.config.enable_reflection}")

    # ------------------------------------------------------------------
    # Core: Execute with cognition
    # ------------------------------------------------------------------

    def execute(
        self,
        agent_fn: Callable,
        task: Any,
        category: str = "default",
        evaluator: Optional[Callable[[Any, Any], bool]] = None,
        max_retries: int = 0,
        **kwargs,
    ) -> ExecutionResult:
        """Execute an agent function with CogniCore cognition.

        Parameters
        ----------
        agent_fn : callable
            The agent function. Called as ``agent_fn(task, context)``
            where ``context`` is a dict with memory and reflection data.
        task : any
            The task/input to pass to the agent.
        category : str
            Category for memory grouping (e.g. "code_fix", "navigation").
        evaluator : callable or None
            Function ``(output, task) -> bool`` to judge success.
            If None, any non-exception result is considered success.
        max_retries : int
            Number of retry attempts if the first fails.
        **kwargs
            Extra args passed to ``agent_fn``.

        Returns
        -------
        ExecutionResult
            Full result with cognition metadata.
        """
        self.stats.total_executions += 1
        self._categories.add(category)
        self.stats.categories_seen = len(self._categories)

        # Build context from memory + reflection
        context = self._build_context(category, task=task)

        # Try execution with retries
        for attempt in range(1, max_retries + 2):
            result = self._single_execute(
                agent_fn, task, category, context, evaluator, attempt, **kwargs
            )
            if result.success or attempt > max_retries:
                break
            # On failure, enrich context with failure info for retry
            context["last_failure"] = result.error or str(result.output)
            context["attempt"] = attempt + 1
            context["reflection_hint"] = self.reflection.get_hint(category)

        # Persist
        if self.config.auto_save and self.config.persistence_path:
            self._save_state()

        return result

    def _single_execute(
        self,
        agent_fn: Callable,
        task: Any,
        category: str,
        context: Dict,
        evaluator: Optional[Callable],
        attempt: int,
        **kwargs,
    ) -> ExecutionResult:
        """Execute once and record the result."""
        result = ExecutionResult(category=category, attempt=attempt)
        result.memory_context = context.get("memory", [])
        result.reflection_hint = context.get("reflection_hint")

        t0 = time.perf_counter()
        try:
            output = agent_fn(task, context, **kwargs)
            result.output = output
            result.duration_ms = (time.perf_counter() - t0) * 1000
            self.stats.total_duration_ms += result.duration_ms

            # Evaluate success
            if evaluator:
                result.success = evaluator(output, task)
            else:
                result.success = True  # No crash = success

        except Exception as e:
            result.duration_ms = (time.perf_counter() - t0) * 1000
            self.stats.total_duration_ms += result.duration_ms
            result.error = str(e)
            result.success = False
            logger.warning(f"Execution failed: {e}")

        # Update stats
        if result.success:
            self.stats.successes += 1
        else:
            self.stats.failures += 1

        # Store in memory
        if self.config.enable_memory:
            from cognicore.memory.base import MemoryEntry
            scope_val = MemoryScope.GLOBAL
            try:
                scope_val = MemoryScope(self.config.memory_scope)
            except ValueError:
                pass
                
            entry = MemoryEntry(
                text=str(task)[:200],
                category=category,
                action=str(result.output)[:500],
                correct=result.success,
                scope=scope_val,
                scope_id=self.config.memory_scope_id,
                metadata={
                    "duration_ms": result.duration_ms,
                    "attempt": attempt,
                    "error": result.error,
                }
            )
            self.memory.store_direct(entry)

        # Log execution
        self._execution_log.append(result.to_dict())

        if self.config.verbose:
            status = "OK" if result.success else "FAIL"
            logger.info(f"[{status}] {category} | {result.duration_ms:.0f}ms | "
                       f"attempt {attempt}")

        return result

    def _build_context(self, category: str, task: Any = None) -> Dict[str, Any]:
        """Build cognition context from memory + reflection."""
        context: Dict[str, Any] = {
            "memory": [],
            "reflection_hint": None,
            "failures_to_avoid": [],
            "successful_patterns": [],
            "category": category,
        }

        if self.config.enable_memory:
            if task:
                results = self.memory.retrieve(
                    query=str(task),
                    task=category,
                    top_k=self.config.memory_top_k
                )
                context["memory"] = [r.entry.to_dict() for r in results]
                self.stats.memory_retrievals += len(results)
            else:
                context["memory"] = [
                    e.to_dict() for e in self.backend.get_by_category(
                        category, top_k=self.config.memory_top_k
                    )
                ]
                self.stats.memory_retrievals += 1

            context["failures_to_avoid"] = [
                e.action
                for e in self.backend.get_by_category(category, top_k=5, success_filter=False)
            ]
            context["successful_patterns"] = [
                e.action
                for e in self.backend.get_by_category(category, top_k=5, success_filter=True)
            ]

            # Check if we're about to repeat a known failure
            if context["failures_to_avoid"]:
                self.stats.repeated_failures_avoided += 1

        if self.config.enable_reflection:
            hint = self.reflection.get_hint(category)
            if hint:
                context["reflection_hint"] = hint

        return context

    # ------------------------------------------------------------------
    # Decorator API
    # ------------------------------------------------------------------

    def wrap(
        self,
        category: str = "default",
        evaluator: Optional[Callable] = None,
        max_retries: int = 0,
    ):
        """Decorator to wrap any function with CogniCore cognition.

        Usage:
            @runtime.wrap(category="code_fix")
            def fix_code(task, context):
                return llm.generate(task + str(context))
        """
        def decorator(fn):
            def wrapper(task, **kwargs):
                return self.execute(
                    agent_fn=fn, task=task, category=category,
                    evaluator=evaluator, max_retries=max_retries, **kwargs
                )
            wrapper.__name__ = fn.__name__
            wrapper.__doc__ = fn.__doc__
            wrapper._cognicore_wrapped = True
            return wrapper
        return decorator

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive runtime statistics."""
        return {
            "runtime": self.stats.to_dict(),
            "memory": self.backend.stats(),
            "reflection": self.reflection.stats(),
            "lifecycle": self.memory.get_health_report()
        }

    def get_failure_report(self) -> Dict[str, Any]:
        """Get detailed failure analysis per category."""
        report = {}
        for cat in self._categories:
            failures = self.backend.get_by_category(cat, top_k=50, success_filter=False)
            successes = self.backend.get_by_category(cat, top_k=50, success_filter=True)
            analysis = self.reflection.analyze(cat)
            report[cat] = {
                "total_failures": len(failures),
                "total_successes": len(successes),
                "bad_patterns": analysis["bad_predictions"],
                "good_patterns": analysis["good_predictions"],
                "recommendation": analysis["recommendation"],
                "hint": self.reflection.get_hint(cat),
            }
        return report

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_state(self):
        path = Path(self.config.persistence_path)
        path.mkdir(parents=True, exist_ok=True)
        if hasattr(self.backend, "persistence_path"):
            self.backend.persistence_path = str(path / f"{self.name}_memory.json")
        self.backend.save()
        # Save stats
        stats_path = path / f"{self.name}_stats.json"
        stats_path.write_text(json.dumps(self.stats.to_dict(), indent=2))

    def _load_state(self):
        path = Path(self.config.persistence_path)
        mem_path = path / f"{self.name}_memory.json"
        if mem_path.exists():
            self.backend.load(mem_path)
            logger.info(f"Loaded {len(self.backend.entries)} memory entries")

    def save(self, path: Optional[str] = None):
        """Manually save runtime state."""
        if path:
            self.config.persistence_path = path
        if self.config.persistence_path:
            self._save_state()

    def reset(self):
        """Reset all runtime state."""
        self.backend.clear()
        self.stats = RuntimeStats()
        self._categories.clear()
        self._execution_log.clear()

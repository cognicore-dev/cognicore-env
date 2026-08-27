"""
CogniCore Benchmark Suite — measures memory impact on agent performance.

Zero heavy dependencies — uses only cognicore.make() + AutoLearner.
No torch, no SB3, no API keys required.

Usage::

    # From CLI
    cognicore bench run
    cognicore bench run --quick
    cognicore bench run --output results.json

    # From Python
    from cognicore.benchmarks import BenchmarkSuite
    suite = BenchmarkSuite()
    result = suite.run()
    print(result.summary())
"""

from __future__ import annotations

import json
import csv
import time
import logging
from dataclasses import dataclass, field, asdict
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("cognicore.benchmarks")

# ── Environments to benchmark ───────────────────────────────────────
DEFAULT_ENVS = [
    "SafetyClassification-v1",
    "CodeDebugging-v1",
    "Planning-v1",
]

DEFAULT_DIFFICULTIES = ["easy", "medium", "hard"]


@dataclass
class EnvResult:
    """Result for a single environment + difficulty + condition."""
    env_id: str
    difficulty: str
    condition: str  # "baseline" or "memory"
    episodes: int = 0
    total_accuracy: float = 0.0
    total_reward: float = 0.0
    solve_count: int = 0
    episode_accuracies: List[float] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def avg_accuracy(self) -> float:
        return self.total_accuracy / self.episodes if self.episodes else 0.0

    @property
    def avg_reward(self) -> float:
        return self.total_reward / self.episodes if self.episodes else 0.0

    @property
    def solve_rate(self) -> float:
        return self.solve_count / self.episodes if self.episodes else 0.0


@dataclass
class BackendResult:
    """Result for a memory backend latency comparison."""
    backend_name: str
    store_ops: int = 0
    search_ops: int = 0
    store_avg_ms: float = 0.0
    search_avg_ms: float = 0.0
    category_avg_ms: float = 0.0


@dataclass
class BenchmarkResult:
    """Full benchmark suite result."""
    version: str = ""
    timestamp: str = ""
    seed: int = 42
    env_results: List[EnvResult] = field(default_factory=list)
    backend_results: List[BackendResult] = field(default_factory=list)
    total_duration_s: float = 0.0

    def summary(self) -> str:
        """Generate a human-readable summary."""
        lines = [
            f"\n{'=' * 60}",
            f"  CogniCore Benchmark Report",
            f"  Version: {self.version} | Seed: {self.seed}",
            f"  Duration: {self.total_duration_s:.1f}s",
            f"{'=' * 60}",
            "",
        ]

        # Memory ablation results
        baseline = [r for r in self.env_results if r.condition == "baseline"]
        memory = [r for r in self.env_results if r.condition == "memory"]

        if baseline and memory:
            b_acc = sum(r.avg_accuracy for r in baseline) / len(baseline)
            m_acc = sum(r.avg_accuracy for r in memory) / len(memory)
            b_solve = sum(r.solve_rate for r in baseline) / len(baseline)
            m_solve = sum(r.solve_rate for r in memory) / len(memory)

            lines.append("  Memory Ablation:")
            lines.append(f"  {'Metric':<20} {'Baseline':>10} {'Memory':>10} {'Delta':>10}")
            lines.append(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10}")
            lines.append(f"  {'Avg Accuracy':<20} {b_acc:>9.1%} {m_acc:>9.1%} {m_acc - b_acc:>+9.1%}")
            lines.append(f"  {'Solve Rate':<20} {b_solve:>9.1%} {m_solve:>9.1%} {m_solve - b_solve:>+9.1%}")
            lines.append("")

            # Per-env breakdown
            lines.append("  Per-Environment (Memory condition):")
            lines.append(f"  {'Environment':<30} {'Accuracy':>10} {'Solve':>10} {'Reward':>10}")
            lines.append(f"  {'-'*30} {'-'*10} {'-'*10} {'-'*10}")
            for r in memory:
                label = f"{r.env_id} ({r.difficulty})"
                lines.append(f"  {label:<30} {r.avg_accuracy:>9.1%} {r.solve_rate:>9.1%} {r.avg_reward:>+9.2f}")
            lines.append("")

        # Learning curves
        memory_easy = [r for r in self.env_results
                       if r.condition == "memory" and r.difficulty == "easy"
                       and r.episode_accuracies]
        if memory_easy:
            lines.append("  Learning Curves (Easy, Memory):")
            for r in memory_easy:
                accs = [f"{a:.0%}" for a in r.episode_accuracies[:5]]
                lines.append(f"    {r.env_id}: {' -> '.join(accs)}")
            lines.append("")

        # Backend latency
        if self.backend_results:
            lines.append("  Backend Latency:")
            lines.append(f"  {'Backend':<20} {'Store':>10} {'Search':>10} {'Category':>10}")
            lines.append(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10}")
            for b in self.backend_results:
                lines.append(
                    f"  {b.backend_name:<20} "
                    f"{b.store_avg_ms:>8.2f}ms "
                    f"{b.search_avg_ms:>8.2f}ms "
                    f"{b.category_avg_ms:>8.2f}ms"
                )
            lines.append("")

        lines.append(f"{'=' * 60}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "seed": self.seed,
            "total_duration_s": self.total_duration_s,
            "env_results": [asdict(r) for r in self.env_results],
            "backend_results": [asdict(r) for r in self.backend_results],
        }

    def to_json(self, path: Optional[str] = None) -> str:
        data = json.dumps(self.to_dict(), indent=2)
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(data, encoding="utf-8")
        return data

    def to_csv(self, path: Optional[str] = None) -> str:
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "env_id", "difficulty", "condition", "episodes",
            "avg_accuracy", "avg_reward", "solve_rate", "duration_ms",
        ])
        for r in self.env_results:
            writer.writerow([
                r.env_id, r.difficulty, r.condition, r.episodes,
                f"{r.avg_accuracy:.4f}", f"{r.avg_reward:.4f}",
                f"{r.solve_rate:.4f}", f"{r.duration_ms:.1f}",
            ])
        data = buf.getvalue()
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(data, encoding="utf-8")
        return data


class BenchmarkSuite:
    """Lightweight benchmark suite for CogniCore.

    Measures the impact of episodic memory on agent performance
    across multiple environments and difficulties. Zero heavy deps.

    Parameters
    ----------
    envs : list of str or None
        Environment IDs to benchmark. Defaults to 3 core envs.
    difficulties : list of str or None
        Difficulties to test. Defaults to ["easy", "medium", "hard"].
    episodes : int
        Episodes per configuration.
    seed : int
        Random seed for reproducibility.
    """

    def __init__(
        self,
        envs: Optional[List[str]] = None,
        difficulties: Optional[List[str]] = None,
        episodes: int = 5,
        seed: int = 42,
    ):
        self.envs = envs or DEFAULT_ENVS
        self.difficulties = difficulties or DEFAULT_DIFFICULTIES
        self.episodes = episodes
        self.seed = seed

    def run(self, quick: bool = False, skip_backend: bool = False) -> BenchmarkResult:
        """Run the full benchmark suite.

        Parameters
        ----------
        quick : bool
            If True, run only easy difficulty with 2 episodes.
        skip_backend : bool
            If True, skip backend latency comparison.
        """
        import cognicore

        difficulties = ["easy"] if quick else self.difficulties
        episodes = min(2, self.episodes) if quick else self.episodes

        result = BenchmarkResult(
            version=getattr(cognicore, "__version__", "unknown"),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            seed=self.seed,
        )

        t0 = time.perf_counter()

        # ── Memory ablation ──────────────────────────────────────────
        for env_id in self.envs:
            for diff in difficulties:
                for condition in ["baseline", "memory"]:
                    env_result = self._run_condition(
                        env_id, diff, condition, episodes, cognicore
                    )
                    result.env_results.append(env_result)

        # ── Backend latency comparison ───────────────────────────────
        if not skip_backend:
            result.backend_results = self._benchmark_backends()

        result.total_duration_s = time.perf_counter() - t0
        return result

    def _run_condition(
        self,
        env_id: str,
        difficulty: str,
        condition: str,
        episodes: int,
        cognicore_mod,
    ) -> EnvResult:
        """Run one env+difficulty+condition combination."""
        er = EnvResult(
            env_id=env_id,
            difficulty=difficulty,
            condition=condition,
        )

        t0 = time.perf_counter()

        try:
            agent = cognicore_mod.AutoLearner()

            for ep in range(episodes):
                if condition == "baseline":
                    # Fresh env each episode — no memory carryover
                    env = cognicore_mod.make(env_id, difficulty=difficulty)
                elif ep == 0 or condition == "baseline":
                    env = cognicore_mod.make(env_id, difficulty=difficulty)

                obs = env.reset()
                ep_correct = 0
                ep_total = 0
                ep_reward = 0.0

                done = False
                while not done:
                    action = agent.act(obs)
                    obs, reward, done, truncated, info = env.step(action)

                    eval_result = info.get("eval_result", {})
                    if eval_result.get("correct"):
                        ep_correct += 1
                    ep_total += 1
                    ep_reward += reward.total if hasattr(reward, "total") else float(reward)

                    if condition == "baseline":
                        agent.learn(reward, info)
                    else:
                        agent.learn(reward, info)

                    if done or truncated:
                        break

                accuracy = ep_correct / ep_total if ep_total else 0.0
                er.episode_accuracies.append(accuracy)
                er.total_accuracy += accuracy
                er.total_reward += ep_reward
                er.episodes += 1

                if accuracy >= 0.8:
                    er.solve_count += 1

        except Exception as exc:
            logger.warning("Benchmark error for %s/%s/%s: %s", env_id, difficulty, condition, exc)

        er.duration_ms = (time.perf_counter() - t0) * 1000
        return er

    def _benchmark_backends(self) -> List[BackendResult]:
        """Compare store/search latency across memory backends."""
        from cognicore.memory.base import MemoryEntry
        from cognicore.memory.tfidf_backend import TFIDFMemoryBackend

        backends = [
            ("TFIDFMemoryBackend", TFIDFMemoryBackend),
        ]

        # Try importing SQLite backend
        try:
            from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
            backends.append(("SQLiteMemoryBackend", SQLiteMemoryBackend))
        except ImportError:
            pass

        results = []
        n_ops = 100

        for name, cls in backends:
            br = BackendResult(backend_name=name, store_ops=n_ops, search_ops=n_ops)

            try:
                if "SQLite" in name:
                    backend = cls(db_path=":memory:")
                else:
                    backend = cls()

                # Store latency
                entries = [
                    MemoryEntry(
                        text=f"Task {i}: fix the bug in module {i % 10}",
                        category=f"cat_{i % 5}",
                        correct=i % 3 != 0,
                        action=f"action_{i % 4}",
                    )
                    for i in range(n_ops)
                ]

                t0 = time.perf_counter()
                for entry in entries:
                    backend.store(entry)
                store_total = (time.perf_counter() - t0) * 1000
                br.store_avg_ms = store_total / n_ops

                # Search latency
                queries = [f"fix bug in module {i % 10}" for i in range(n_ops)]
                t0 = time.perf_counter()
                for q in queries:
                    backend.search(q, top_k=5)
                search_total = (time.perf_counter() - t0) * 1000
                br.search_avg_ms = search_total / n_ops

                # Category retrieval latency
                categories = [f"cat_{i % 5}" for i in range(n_ops)]
                t0 = time.perf_counter()
                for c in categories:
                    backend.get_by_category(c, top_k=5)
                cat_total = (time.perf_counter() - t0) * 1000
                br.category_avg_ms = cat_total / n_ops

            except Exception as exc:
                logger.warning("Backend benchmark error for %s: %s", name, exc)

            results.append(br)

        return results


# ── CLI entry point ──────────────────────────────────────────────────
def _cli_main():
    """Entry point for `python -m cognicore.benchmarks.suite`."""
    import argparse

    parser = argparse.ArgumentParser(description="CogniCore Benchmark Suite")
    parser.add_argument("--quick", action="store_true", help="Quick run (easy only, 2 episodes)")
    parser.add_argument("--episodes", type=int, default=5, help="Episodes per config")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--output", type=str, default=None, help="Output JSON path")
    parser.add_argument("--csv", type=str, default=None, help="Output CSV path")
    parser.add_argument("--skip-backend", action="store_true", help="Skip backend latency test")
    args = parser.parse_args()

    suite = BenchmarkSuite(episodes=args.episodes, seed=args.seed)
    print("\n  Running CogniCore benchmarks...")
    result = suite.run(quick=args.quick, skip_backend=args.skip_backend)
    print(result.summary())

    if args.output:
        result.to_json(args.output)
        print(f"  Results saved to {args.output}")
    if args.csv:
        result.to_csv(args.csv)
        print(f"  CSV saved to {args.csv}")


if __name__ == "__main__":
    _cli_main()

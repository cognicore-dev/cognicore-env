#!/usr/bin/env python3
"""
Memory System Head-to-Head Benchmark
=====================================
Two clean, non-mixed benchmarks:

  BENCHMARK 1 -- Accuracy (LongMemEval-style)
    Same questions. Same evaluator. Same top-k.
    Evaluator: exact/substring string match (no LLM API needed).

  BENCHMARK 2 -- Retrieval Efficiency
    Tokens injected per query, write/read latency, storage size.

Usage:
    python benchmarks/head_to_head_benchmark.py
"""
from __future__ import annotations

import os, sys, time, json, tempfile, statistics, warnings
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict

warnings.filterwarnings("ignore")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── Token counting ────────────────────────────────────────────────────────────
try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    def count_tokens(text: str) -> int:
        return len(_enc.encode(text))
    TOKEN_METHOD = "tiktoken cl100k"
except ImportError:
    def count_tokens(text: str) -> int:
        return max(1, int(len(text.split()) / 0.75))
    TOKEN_METHOD = "word-count estimate"

# ── CogniCore ─────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from cognicore.memory.base import MemoryEntry, MemoryScope


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LongMemEval Dataset (local, no API required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LONGMEMEVAL: List[Dict] = [
    {"id":"q01","domain":"auth","memory":"User prefers OAuth 2.0 with PKCE for all web auth flows.","question":"What auth protocol does the user prefer?","answer":"OAuth 2.0 with PKCE"},
    {"id":"q02","domain":"auth","memory":"Access tokens must expire in 15 minutes; refresh tokens in 7 days.","question":"How long should access tokens last?","answer":"15 minutes"},
    {"id":"q03","domain":"auth","memory":"Never store tokens in localStorage, always use httpOnly cookies.","question":"Where should tokens be stored?","answer":"httpOnly cookies"},
    {"id":"q04","domain":"auth","memory":"JWT signing algorithm must be RS256; symmetric HS256 is forbidden.","question":"Which JWT signing algorithm is required?","answer":"RS256"},
    {"id":"q05","domain":"auth","memory":"MFA is mandatory for all admin roles.","question":"Is MFA required for admin users?","answer":"yes"},
    {"id":"q06","domain":"react","memory":"User prefers functional React components with hooks, no class components.","question":"Should new React components use hooks?","answer":"yes"},
    {"id":"q07","domain":"react","memory":"State management is Zustand, not Redux.","question":"What state management library does the project use?","answer":"Zustand"},
    {"id":"q08","domain":"react","memory":"All forms use react-hook-form with zod validation.","question":"What library handles form validation?","answer":"zod"},
    {"id":"q09","domain":"react","memory":"User wants strict TypeScript, no 'any' types.","question":"Is the TypeScript 'any' type allowed?","answer":"no"},
    {"id":"q10","domain":"react","memory":"User's design system is based on shadcn/ui components.","question":"Which UI component library does the user use?","answer":"shadcn"},
    {"id":"q11","domain":"database","memory":"Database is PostgreSQL 15 with pgvector extension.","question":"What database does the project use?","answer":"PostgreSQL"},
    {"id":"q12","domain":"database","memory":"ORM is Drizzle ORM, not Prisma.","question":"What ORM is used in the project?","answer":"Drizzle"},
    {"id":"q13","domain":"database","memory":"UUID v4 used as primary keys, not integer sequences.","question":"What type of primary key is used?","answer":"UUID"},
    {"id":"q14","domain":"database","memory":"Redis is used for caching with TTL of 5 minutes for API responses.","question":"What is the cache TTL for API responses?","answer":"5 minutes"},
    {"id":"q15","domain":"database","memory":"Row-level security (RLS) enabled on all tables.","question":"Is row-level security enabled?","answer":"yes"},
    {"id":"q16","domain":"devops","memory":"CI/CD pipeline is GitHub Actions.","question":"What CI/CD system is used?","answer":"GitHub Actions"},
    {"id":"q17","domain":"devops","memory":"Docker image must use node:20-alpine base.","question":"What base image should Docker use?","answer":"node:20-alpine"},
    {"id":"q18","domain":"devops","memory":"Test coverage must stay above 80%.","question":"What is the minimum test coverage threshold?","answer":"80%"},
    {"id":"q19","domain":"devops","memory":"Semantic versioning (semver) required for all releases.","question":"What versioning scheme is required?","answer":"semver"},
    {"id":"q20","domain":"devops","memory":"PR must pass all checks before merge; no direct pushes to main.","question":"Can developers push directly to main?","answer":"no"},
    {"id":"q21","domain":"prefs","memory":"User's preferred language is TypeScript for all new projects.","question":"What language does the user prefer?","answer":"TypeScript"},
    {"id":"q22","domain":"prefs","memory":"User works in VSCode with Vim keybindings.","question":"What editor does the user use?","answer":"VSCode"},
    {"id":"q23","domain":"prefs","memory":"User's timezone is Asia/Kolkata (IST, UTC+5:30).","question":"What timezone is the user in?","answer":"Asia/Kolkata"},
    {"id":"q24","domain":"prefs","memory":"User wants all API responses in camelCase, not snake_case.","question":"Should API responses use camelCase?","answer":"yes"},
    {"id":"q25","domain":"prefs","memory":"User dislikes OOP patterns; prefers functional programming.","question":"Does the user prefer OOP or functional programming?","answer":"functional"},
    {"id":"q26","domain":"multi","memory":"User uses pnpm, not npm or yarn.","question":"Which package manager should be used?","answer":"pnpm"},
    {"id":"q27","domain":"multi","memory":"All secrets stored in GitHub Actions secrets, never in code.","question":"Where should API secrets be stored?","answer":"GitHub Actions secrets"},
    {"id":"q28","domain":"multi","memory":"Soft deletes preferred over hard deletes (deleted_at column).","question":"How should records be deleted?","answer":"soft delete"},
    {"id":"q29","domain":"multi","memory":"Linting with ESLint + Prettier, pre-commit via Husky.","question":"What tool enforces code style on commit?","answer":"Husky"},
    {"id":"q30","domain":"multi","memory":"Connection pooling via pgBouncer with max 20 connections.","question":"What is the max DB connection pool size?","answer":"20"},
]

TOP_K = 5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Evaluator (model-free, reproducible)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def evaluate_answer(retrieved: List[str], expected: str) -> bool:
    needle = expected.lower()
    for mem in retrieved:
        if needle in mem.lower():
            return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Adapters
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SkipAdapter(Exception):
    pass


class CogniCoreFTS5:
    name = "CogniCore (FTS5)"
    arch = "SQLite BM25/FTS5"

    def setup(self):
        from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
        self._tmp = tempfile.mktemp(suffix=".db")
        self.b = SQLiteMemoryBackend(self._tmp)
        self.b._init_db()
        for q in LONGMEMEVAL:
            self.b.store(MemoryEntry(text=q["memory"], category=q["domain"], scope=MemoryScope.GLOBAL))

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]:
        safe = query.replace("?","").replace("/", " ")
        try:
            return [r.entry.text for r in self.b.search(safe, top_k=k, scope=MemoryScope.GLOBAL)]
        except Exception:
            return []

    def storage_bytes(self) -> int:
        try: return os.path.getsize(self._tmp)
        except: return 0

    def teardown(self):
        try: os.unlink(self._tmp)
        except: pass


class CogniCoreHybrid:
    name = "CogniCore (Hybrid)"
    arch = "TF-IDF + FTS5 RRF"

    def setup(self):
        try:
            from cognicore.memory.hybrid_backend import HybridMemoryBackend
            self.b = HybridMemoryBackend()
            for q in LONGMEMEVAL:
                self.b.store(MemoryEntry(text=q["memory"], category=q["domain"]))
        except Exception as e:
            raise SkipAdapter(f"Hybrid unavailable: {e}")

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]:
        try:
            return [r.entry.text for r in self.b.search(query, top_k=k)]
        except Exception:
            return []

    def storage_bytes(self) -> int: return 0
    def teardown(self): pass


class Mem0Local:
    name = "Mem0"
    arch = "Chroma + MiniLM"

    def setup(self):
        try:
            from mem0 import Memory
        except ImportError:
            raise SkipAdapter("pip install mem0ai")
        os.environ.setdefault("OPENAI_API_KEY", "sk-noop-offline")
        self._tmp = tempfile.mkdtemp(prefix="mem0_b2b_")
        cfg = {
            "embedder": {"provider": "huggingface",
                         "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"}},
            "vector_store": {"provider": "chroma",
                             "config": {"collection_name": "b2b", "path": self._tmp}},
        }
        try:
            self.m = Memory.from_config(cfg)
            for q in LONGMEMEVAL:
                self.m.add(q["memory"], user_id="b2b", infer=False)
        except Exception as e:
            raise SkipAdapter(f"Mem0 init failed: {e}")

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]:
        try:
            r = self.m.search(query, top_k=k, filters={"user_id": "b2b"}, threshold=0.0)
            rows = r.get("results", r) if isinstance(r, dict) else r
            return [row.get("memory", "") for row in rows]
        except Exception:
            return []

    def storage_bytes(self) -> int: return 0
    def teardown(self): pass


class ZepSystem:
    name = "Zep (Graphiti)"
    arch = "Temporal knowledge graph"

    def setup(self):
        key = os.environ.get("ZEP_API_KEY")
        if not key:
            raise SkipAdapter("ZEP_API_KEY not set")
        try:
            from zep_cloud.client import Zep  # noqa
        except ImportError:
            raise SkipAdapter("pip install zep-cloud")
        raise SkipAdapter("Live adapter pending (needs graph session wiring)")

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]: return []
    def storage_bytes(self) -> int: return 0
    def teardown(self): pass


class LettaSystem:
    name = "Letta"
    arch = "Agent archival memory"

    def setup(self):
        try:
            from letta import create_client  # noqa
        except ImportError:
            raise SkipAdapter("pip install letta")
        raise SkipAdapter("Letta server not running locally")

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]: return []
    def storage_bytes(self) -> int: return 0
    def teardown(self): pass


class LangMemSystem:
    name = "LangMem"
    arch = "LangGraph agent memory"

    def setup(self):
        try:
            import langmem  # noqa
        except ImportError:
            raise SkipAdapter("pip install langmem")
        raise SkipAdapter("LangMem adapter needs LangGraph wiring")

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]: return []
    def storage_bytes(self) -> int: return 0
    def teardown(self): pass


class SupermemorySystem:
    name = "Supermemory"
    arch = "Cloud semantic memory"

    def setup(self):
        key = os.environ.get("SUPERMEMORY_API_KEY")
        if not key:
            raise SkipAdapter("SUPERMEMORY_API_KEY not set")
        raise SkipAdapter("Live adapter pending")

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]: return []
    def storage_bytes(self) -> int: return 0
    def teardown(self): pass


class FlatDump:
    name = "Naive (no retrieval)"
    arch = "Inject all memories"

    def setup(self):
        self._all = [q["memory"] for q in LONGMEMEVAL]

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]:
        return self._all

    def storage_bytes(self) -> int: return 0
    def teardown(self): pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class BenchRow:
    name: str
    arch: str
    status: str = "ok"
    skip_reason: str = ""
    accuracy: float = 0.0
    correct: int = 0
    total: int = 0
    avg_tokens: float = 0.0
    avg_read_ms: float = 0.0
    write_ms: float = 0.0
    storage_bytes: int = 0
    cost_per_1k: float = 0.0


def run_adapter(adapter) -> BenchRow:
    row = BenchRow(name=adapter.name, arch=adapter.arch)

    t0 = time.perf_counter()
    try:
        adapter.setup()
    except SkipAdapter as e:
        row.status = "skip"; row.skip_reason = str(e); return row
    except Exception as e:
        row.status = "error"; row.skip_reason = f"{type(e).__name__}: {e}"; return row
    row.write_ms = round((time.perf_counter() - t0) * 1000, 1)
    row.storage_bytes = adapter.storage_bytes()

    correct = 0
    token_totals: List[float] = []
    read_times: List[float] = []

    for q in LONGMEMEVAL:
        t1 = time.perf_counter()
        retrieved = adapter.retrieve(q["question"], k=TOP_K)
        read_times.append((time.perf_counter() - t1) * 1000)
        token_totals.append(count_tokens("\n".join(retrieved)))
        if evaluate_answer(retrieved, q["answer"]):
            correct += 1

    row.total = len(LONGMEMEVAL)
    row.correct = correct
    row.accuracy = correct / row.total
    row.avg_tokens = statistics.mean(token_totals)
    row.avg_read_ms = round(statistics.mean(read_times), 2)
    row.cost_per_1k = round(row.avg_tokens / 1_000_000 * 3.0 * 1000, 4)

    adapter.teardown()
    return row


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Output tables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def SEP(n=80): return "=" * n
def sep(n=80): return "-" * n

def main():
    ADAPTERS = [
        CogniCoreFTS5(), CogniCoreHybrid(),
        Mem0Local(),
        ZepSystem(), LettaSystem(), LangMemSystem(), SupermemorySystem(),
        FlatDump(),
    ]

    print(); print(SEP())
    print("  Memory System Head-to-Head Benchmark")
    print(f"  Dataset : {len(LONGMEMEVAL)} LongMemEval QA pairs, {len(set(q['domain'] for q in LONGMEMEVAL))} domains")
    print(f"  Top-K   : {TOP_K}   |   Evaluator: substring match (no LLM API)")
    print(SEP())

    rows: List[BenchRow] = []
    for adapter in ADAPTERS:
        print(f"  [{adapter.name}]...", end=" ", flush=True)
        r = run_adapter(adapter)
        rows.append(r)
        if r.status == "ok":
            print(f"acc={r.accuracy:.0%}  tokens={r.avg_tokens:.0f}  {r.avg_read_ms:.1f}ms")
        elif r.status == "skip":
            print(f"SKIP -> {r.skip_reason}")
        else:
            print(f"ERROR -> {r.skip_reason}")

    # ── BENCHMARK 1 ──────────────────────────────────────────────────────────
    print(); print(SEP())
    print("  BENCHMARK 1  --  LongMemEval Accuracy")
    print("  (same questions, same evaluator, same top-k for every system)")
    print(SEP())
    print(f"  {'System':<26} {'Architecture':<28} {'Accuracy':>9} {'Correct':>9}/{len(LONGMEMEVAL)}")
    print(sep())
    for r in rows:
        if r.status == "ok":
            print(f"  {r.name:<26} {r.arch:<28} {r.accuracy:>8.1%} {r.correct:>8}")
        else:
            tag = "SKIP" if r.status == "skip" else "FAIL"
            reason = r.skip_reason[:46]
            print(f"  {r.name:<26} [{tag}] {reason}")
    print(SEP())

    # ── BENCHMARK 2 ──────────────────────────────────────────────────────────
    print(); print(SEP())
    print("  BENCHMARK 2  --  Retrieval Efficiency")
    print(f"  Token method: {TOKEN_METHOD}   |   Cost: $3/1M tokens (Claude Sonnet 3.5)")
    print(SEP())
    print(f"  {'System':<26} {'Avg Tokens/Q':>13} {'Read ms':>9} {'Write ms':>10} {'$/1K queries':>14}")
    print(sep())
    for r in rows:
        if r.status == "ok":
            print(f"  {r.name:<26} {r.avg_tokens:>13.0f} {r.avg_read_ms:>9.2f} {r.write_ms:>10.0f} {r.cost_per_1k:>14.4f}")
        else:
            tag = "SKIP" if r.status == "skip" else "FAIL"
            print(f"  {r.name:<26} [{tag}] {r.skip_reason[:52]}")
    print(SEP())

    # ── Summary ───────────────────────────────────────────────────────────────
    ok = [r for r in rows if r.status == "ok"]
    if ok:
        best_acc = max(ok, key=lambda r: r.accuracy)
        best_tok = min(ok, key=lambda r: r.avg_tokens)
        best_spd = min(ok, key=lambda r: r.avg_read_ms)

        print(); print(SEP())
        print("  COMBINED SUMMARY")
        print(SEP())
        print(f"  {'System':<26} {'Accuracy':>9} {'Tokens/Q':>10} {'Latency':>9} {'$/1K Q':>9}")
        print(sep())
        for r in ok:
            flags = ""
            if r is best_acc: flags += " [best accuracy]"
            if r is best_tok: flags += " [fewest tokens]"
            if r is best_spd: flags += " [fastest]"
            print(f"  {r.name:<26} {r.accuracy:>8.1%} {r.avg_tokens:>10.0f} {r.avg_read_ms:>8.2f}ms {r.cost_per_1k:>8.4f}{flags}")
        print(SEP())

        cogni = [r for r in ok if "CogniCore" in r.name]
        others = [r for r in ok if "CogniCore" not in r.name and "Naive" not in r.name]
        if cogni and others:
            bc = max(cogni, key=lambda r: r.accuracy)
            bo = max(others, key=lambda r: r.accuracy)
            avg_tok_o = statistics.mean(r.avg_tokens for r in others)
            tok_delta = (1 - bc.avg_tokens / max(avg_tok_o, 1)) * 100
            acc_delta = (bc.accuracy - bo.accuracy) * 100

            print()
            print(f"  KEY CLAIMS:")
            sign = "+" if acc_delta >= 0 else ""
            print(f"    Accuracy : CogniCore {bc.accuracy:.0%} vs {bo.name} {bo.accuracy:.0%}  ({sign}{acc_delta:.1f} pp)")
            print(f"    Tokens   : CogniCore {bc.avg_tokens:.0f} avg vs others avg {avg_tok_o:.0f}  ({tok_delta:+.1f}%)")
            savings = (avg_tok_o - bc.avg_tokens) / 1_000_000 * 3.0 * 100_000
            print(f"    Cost     : ~${savings:.2f} saved per 100K queries/month")
            print(SEP())

    # Save
    out = Path(__file__).parent.parent / "benchmark_output" / "head_to_head_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump([asdict(r) for r in rows], f, indent=2)
    print(f"\n  Saved -> {out}\n")


if __name__ == "__main__":
    main()

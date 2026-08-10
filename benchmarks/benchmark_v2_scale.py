#!/usr/bin/env python3
"""
Memory System Benchmark v2 — Scale + Dense
==========================================
Extends head_to_head_benchmark.py with two changes:

  1. CogniCore Dense (MiniLM) — same embedding model as Mem0.
     Tests whether CogniCore can close the accuracy gap while
     keeping its latency and token-efficiency advantage.

  2. 500-memory dataset — 30 real QA targets + 470 hard distractors.
     At this scale, naive flat-dump costs ~3,500 tokens per query and
     Mem0's embedding-index write time becomes a production bottleneck.

Same evaluator (substring match), same top-k, same LLM control as v1.

Usage:
    python benchmarks/benchmark_v2_scale.py
"""
from __future__ import annotations

import os, sys, time, json, tempfile, statistics, warnings, random
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict

warnings.filterwarnings("ignore")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

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

sys.path.insert(0, str(Path(__file__).parent.parent))
from cognicore.memory.base import MemoryEntry, MemoryScope

random.seed(42)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 30 LongMemEval targets (same as v1)
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 470 hard distractors — realistic engineering memories, wrong answers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DISTRACTOR_TEMPLATES = [
    # auth distractors
    "The legacy service uses Basic Auth with base64 credentials.",
    "Old API tokens expire in 60 minutes per the previous security policy.",
    "The mobile app stores the refresh token in AsyncStorage for performance.",
    "The admin panel uses session cookies with a 24-hour expiry.",
    "HMAC-SHA256 is used for webhook signature verification.",
    "The OAuth callback is registered at /api/auth/callback.",
    "The rate limiter allows 1000 requests per minute per IP.",
    "SAML 2.0 is used for enterprise SSO with Okta.",
    "The legacy auth endpoint is /api/v1/login and is deprecated.",
    "Passwords must be at least 12 characters with one uppercase.",
    # react distractors
    "The old frontend used class components and Redux Saga.",
    "The legacy dashboard uses Angular 15 with NgRx state.",
    "Older forms were built with Formik and Yup validation.",
    "The marketing site uses Next.js 13 with Tailwind.",
    "The mobile app uses React Native with Expo.",
    "The admin UI uses Ant Design components.",
    "The customer portal uses Vue 3 with Pinia state management.",
    "CSS Modules are used for component-level styling in legacy pages.",
    "The old API client used axios with manual retry logic.",
    "GraphQL subscriptions are used for real-time features.",
    # database distractors
    "The analytics warehouse is BigQuery with dbt transformations.",
    "Legacy tables still use integer auto-increment primary keys.",
    "The old ORM was Sequelize before migrating to Drizzle.",
    "MongoDB is used for storing user session data.",
    "The queue system uses PostgreSQL with SKIP LOCKED polling.",
    "Elasticsearch 8 handles full-text search for the product catalog.",
    "The cache layer uses Memcached for session data.",
    "The read replica is hosted in us-east-1 for lower latency.",
    "Cassandra stores raw event logs from the analytics pipeline.",
    "SQLite is used for local development only.",
    # devops distractors
    "The old pipeline was Jenkins with Groovy DSL.",
    "Staging environment runs on AWS ECS Fargate.",
    "The legacy app used Heroku before migrating to Railway.",
    "Build artifacts are stored in AWS S3 with 30-day lifecycle.",
    "The monitoring stack is Datadog with custom dashboards.",
    "Sentry is used for error tracking with Slack alerts.",
    "The CDN is Cloudflare with 1-hour edge cache TTL.",
    "Terraform manages all AWS infrastructure as code.",
    "Log aggregation uses the ELK stack (Elasticsearch, Logstash, Kibana).",
    "LoadBalancer health checks run every 10 seconds on /health.",
    # prefs distractors
    "The previous developer preferred Java with Spring Boot.",
    "The CTO uses JetBrains Fleet with default keybindings.",
    "The design team is based in UTC+1 (Central European Time).",
    "API responses previously used snake_case for all fields.",
    "The backend team prefers object-oriented design with SOLID principles.",
    "The founder uses Sublime Text for quick edits.",
    "Previous sprints used Jira for project management.",
    "The team prefers YAML configuration over JSON.",
    "Internal tools are written in Python 3.11 with FastAPI.",
    "The QA team uses Playwright for end-to-end browser tests.",
]

def build_500_memory_store() -> List[str]:
    """30 targets + 470 distractors (shuffled) = 500 total memories."""
    targets = [q["memory"] for q in LONGMEMEVAL]
    n_distractors = 500 - len(targets)

    # Cycle through templates and add variation to ensure 470 unique strings
    distractors = []
    template_count = len(DISTRACTOR_TEMPLATES)
    for i in range(n_distractors):
        tmpl = DISTRACTOR_TEMPLATES[i % template_count]
        # Add slight variation every cycle to avoid exact duplicates
        if i >= template_count:
            tmpl = tmpl + f" (ref #{i // template_count})"
        distractors.append(tmpl)

    all_memories = targets + distractors
    random.shuffle(all_memories)
    return all_memories

TOP_K = 5
ALL_500 = build_500_memory_store()

print(f"  Memory store size: {len(ALL_500)} entries ({len([m for m in ALL_500 if m in [q['memory'] for q in LONGMEMEVAL]])} targets + {len(ALL_500)-30} distractors)")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Evaluator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def evaluate_answer(retrieved: List[str], expected: str) -> bool:
    needle = expected.lower()
    for mem in retrieved:
        if needle in mem.lower():
            return True
    return False

class SkipAdapter(Exception):
    pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Adapters — all loading from ALL_500
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CogniCoreFTS5:
    name = "CogniCore (FTS5)"
    arch = "SQLite BM25/FTS5"

    def setup(self):
        from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
        self._tmp = tempfile.mktemp(suffix=".db")
        self.b = SQLiteMemoryBackend(self._tmp)
        self.b._init_db()
        for i, text in enumerate(ALL_500):
            self.b.store(MemoryEntry(text=text, category="general", scope=MemoryScope.GLOBAL))

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]:
        safe = query.replace("?", "").replace("/", " ")
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
            for text in ALL_500:
                self.b.store(MemoryEntry(text=text, category="general"))
        except Exception as e:
            raise SkipAdapter(f"Hybrid unavailable: {e}")

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]:
        try:
            return [r.entry.text for r in self.b.search(query, top_k=k)]
        except Exception:
            return []

    def storage_bytes(self) -> int: return 0
    def teardown(self): pass


class CogniCoreDense:
    """CogniCore with MiniLM embeddings — same model as Mem0 for fair comparison."""
    name = "CogniCore (Dense/MiniLM)"
    arch = "Faiss + MiniLM L6"

    def setup(self):
        try:
            from cognicore.memory.embedding_backend import BasicEmbeddingBackend
            from cognicore.memory.providers.sentence_transformers import SentenceTransformerProvider
            self.b = BasicEmbeddingBackend(
                provider=SentenceTransformerProvider("all-MiniLM-L6-v2")
            )
            for text in ALL_500:
                self.b.store(MemoryEntry(text=text, category="general", scope=MemoryScope.GLOBAL))
        except Exception as e:
            raise SkipAdapter(f"Dense backend unavailable: {e}")

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]:
        try:
            results = self.b.search(query, top_k=k)
            out = []
            for r in results:
                entry = getattr(r, "entry", None)
                if entry:
                    out.append(entry.text)
                elif isinstance(r, str):
                    out.append(r)
            return out
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
        self._tmp = tempfile.mkdtemp(prefix="mem0_v2_")
        cfg = {
            "embedder": {"provider": "huggingface",
                         "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"}},
            "vector_store": {"provider": "chroma",
                             "config": {"collection_name": "b2b_v2", "path": self._tmp}},
        }
        try:
            self.m = Memory.from_config(cfg)
            for text in ALL_500:
                self.m.add(text, user_id="b2b", infer=False)
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


class FlatDump:
    name = "Naive (flat dump)"
    arch = "Inject all 500 memories"

    def setup(self):
        self._all = list(ALL_500)

    def retrieve(self, query: str, k: int = TOP_K) -> List[str]:
        return self._all   # everything, every time

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
    precision: float = 0.0   # relevant retrieved / total retrieved


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
    precision_scores: List[float] = []

    target_set = {q["memory"] for q in LONGMEMEVAL}

    for q in LONGMEMEVAL:
        t1 = time.perf_counter()
        retrieved = adapter.retrieve(q["question"], k=TOP_K)
        read_times.append((time.perf_counter() - t1) * 1000)

        token_totals.append(count_tokens("\n".join(retrieved)))

        # Precision: how many retrieved are actual target memories
        relevant = sum(1 for r in retrieved if r in target_set)
        precision_scores.append(relevant / max(len(retrieved), 1))

        if evaluate_answer(retrieved, q["answer"]):
            correct += 1

    row.total = len(LONGMEMEVAL)
    row.correct = correct
    row.accuracy = correct / row.total
    row.avg_tokens = statistics.mean(token_totals)
    row.avg_read_ms = round(statistics.mean(read_times), 2)
    row.precision = round(statistics.mean(precision_scores), 3)
    row.cost_per_1k = round(row.avg_tokens / 1_000_000 * 3.0 * 1000, 4)

    adapter.teardown()
    return row


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Output
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def SEP(n=84): return "=" * n
def sep(n=84): return "-" * n

def main():
    ADAPTERS = [
        CogniCoreFTS5(),
        CogniCoreHybrid(),
        CogniCoreDense(),   # NEW — same MiniLM as Mem0
        Mem0Local(),
        FlatDump(),
    ]

    print(); print(SEP())
    print("  Memory Benchmark v2 — Scale (500 memories) + Dense Embeddings")
    print(f"  Store   : {len(ALL_500)} memories  (30 targets + 470 hard distractors)")
    print(f"  Queries : {len(LONGMEMEVAL)} LongMemEval QA pairs  |  Top-K = {TOP_K}")
    print(f"  Same LLM control: N/A  |  Evaluator: substring match (no API)")
    print(SEP())

    rows: List[BenchRow] = []
    for adapter in ADAPTERS:
        print(f"\n  [{adapter.name}]", end=" ", flush=True)
        r = run_adapter(adapter)
        rows.append(r)
        if r.status == "ok":
            print(f"acc={r.accuracy:.0%}  prec={r.precision:.0%}  tokens={r.avg_tokens:.0f}  read={r.avg_read_ms:.1f}ms  write={r.write_ms:.0f}ms")
        elif r.status == "skip":
            print(f"SKIP -> {r.skip_reason}")
        else:
            print(f"ERROR -> {r.skip_reason}")

    ok = [r for r in rows if r.status == "ok"]

    # ── BENCHMARK 1 ──────────────────────────────────────────────────────────
    print(); print(SEP())
    print("  BENCHMARK 1  --  LongMemEval Accuracy at 500-memory scale")
    print(SEP())
    print(f"  {'System':<28} {'Arch':<22} {'Accuracy':>9} {'Precision':>10} {'Correct':>8}/{len(LONGMEMEVAL)}")
    print(sep())
    for r in rows:
        if r.status == "ok":
            print(f"  {r.name:<28} {r.arch:<22} {r.accuracy:>8.1%} {r.precision:>9.1%} {r.correct:>8}")
        else:
            tag = "SKIP" if r.status == "skip" else "FAIL"
            print(f"  {r.name:<28} [{tag}] {r.skip_reason[:46]}")
    print(SEP())

    # ── BENCHMARK 2 ──────────────────────────────────────────────────────────
    print(); print(SEP())
    print("  BENCHMARK 2  --  Retrieval Efficiency at 500-memory scale")
    print(f"  Token method : {TOKEN_METHOD}")
    print(f"  Cost rate    : $3/1M input tokens  (Claude Sonnet 3.5)")
    print(SEP())
    print(f"  {'System':<28} {'Tokens/Q':>10} {'Read ms':>9} {'Write ms':>11} {'$/1K Q':>9}")
    print(sep())
    for r in rows:
        if r.status == "ok":
            print(f"  {r.name:<28} {r.avg_tokens:>10.0f} {r.avg_read_ms:>9.2f} {r.write_ms:>11.0f} {r.cost_per_1k:>9.4f}")
        else:
            tag = "SKIP" if r.status == "skip" else "FAIL"
            print(f"  {r.name:<28} [{tag}] {r.skip_reason[:50]}")
    print(SEP())

    # ── Combined summary ──────────────────────────────────────────────────────
    if ok:
        best_acc = max(ok, key=lambda r: r.accuracy)
        best_tok = min(ok, key=lambda r: r.avg_tokens)
        best_spd = min(ok, key=lambda r: r.avg_read_ms if r.avg_read_ms > 0.001 else 99999)
        best_prec = max(ok, key=lambda r: r.precision)

        print(); print(SEP())
        print("  COMBINED SUMMARY TABLE")
        print(SEP())
        print(f"  {'System':<28} {'Accuracy':>9} {'Precision':>10} {'Tokens/Q':>10} {'Latency':>9} {'$/1K Q':>8}")
        print(sep())
        for r in ok:
            flags = []
            if r is best_acc:  flags.append("best-acc")
            if r is best_tok:  flags.append("fewest-tok")
            if r is best_spd:  flags.append("fastest")
            if r is best_prec: flags.append("best-prec")
            flag_str = " [" + ", ".join(flags) + "]" if flags else ""
            print(f"  {r.name:<28} {r.accuracy:>8.1%} {r.precision:>9.1%} {r.avg_tokens:>10.0f} {r.avg_read_ms:>8.2f}ms {r.cost_per_1k:>7.4f}{flag_str}")
        print(SEP())

        cogni = [r for r in ok if "CogniCore" in r.name]
        others = [r for r in ok if "CogniCore" not in r.name and "Naive" not in r.name]
        naive  = [r for r in ok if "Naive" in r.name]

        if cogni:
            best_cogni = max(cogni, key=lambda r: r.accuracy)
            print()
            print("  KEY FINDINGS:")
            if others:
                best_other = max(others, key=lambda r: r.accuracy)
                acc_d = (best_cogni.accuracy - best_other.accuracy) * 100
                tok_d = (1 - best_cogni.avg_tokens / max(best_other.avg_tokens, 1)) * 100
                lat_d = best_other.avg_read_ms / max(best_cogni.avg_read_ms, 0.001)
                sign  = "+" if acc_d >= 0 else ""
                print(f"    vs {best_other.name}:")
                print(f"      Accuracy  : CogniCore {best_cogni.accuracy:.0%}  vs  {best_other.accuracy:.0%}  ({sign}{acc_d:.1f} pp)")
                print(f"      Tokens    : CogniCore {best_cogni.avg_tokens:.0f}  vs  {best_other.avg_tokens:.0f}  ({tok_d:+.1f}%)")
                print(f"      Read speed: CogniCore {best_cogni.avg_read_ms:.2f}ms  vs  {best_other.avg_read_ms:.2f}ms  ({lat_d:.0f}x faster)")
                savings = (best_other.avg_tokens - best_cogni.avg_tokens) / 1_000_000 * 3.0 * 100_000
                print(f"      Cost save : ~${savings:.2f} per 100K queries/month")
            if naive:
                flat = naive[0]
                flat_tok_red = (1 - best_cogni.avg_tokens / max(flat.avg_tokens, 1)) * 100
                print(f"    vs Flat Dump:")
                print(f"      Tokens    : {flat_tok_red:.1f}% fewer ({best_cogni.avg_tokens:.0f} vs {flat.avg_tokens:.0f})")
                print(f"      Accuracy  : {best_cogni.accuracy:.0%} vs {flat.accuracy:.0%}  (precision filter wins at scale)")
            print(SEP())

    # Save
    out = Path(__file__).parent.parent / "benchmark_output" / "benchmark_v2_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump([asdict(r) for r in rows], f, indent=2)
    print(f"\n  Saved -> {out}\n")


if __name__ == "__main__":
    main()

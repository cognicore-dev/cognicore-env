"""Search quality and speed benchmark for SQLiteMemoryBackend.

    python scripts/bench_search.py [--semantic] [--compact] [--filler N ...] [--out results.json]

Loads the labeled set in benchmarks/search/search_set.json into a fresh database,
then adds N filler memories saved *after* it (so the labeled ones are the oldest
rows, as real early decisions are). For every filler size it reports, per query
kind (exact, keyword, paraphrase):

    hit@1   the expected memory is the top result
    R@5     the expected memory is in the top 5
    MRR     mean reciprocal rank within the top 5

plus the tokens returned per search (characters / 4 over the top 5 texts), save
time per memory and search latency (p50 / p95).

--compact runs the recall tools' compact search (cognicore.memory.compact_recall) instead of plain search.
--semantic uses SentenceTransformerProvider (pip install sentence-transformers);
without it the backend runs keyword search only, as the hosted server does today.
Run it before and after a search change and compare the two JSON files.
"""
import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cognicore.memory import compact_recall  # noqa: E402
from cognicore.memory.base import MemoryEntry  # noqa: E402
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend  # noqa: E402

SET_PATH = REPO / "benchmarks" / "search" / "search_set.json"
TOP_K = 5

_SUBJECTS = ["quarterly report", "vendor contract", "design review", "travel booking", "office move",
             "hiring loop", "budget sheet", "team offsite", "brand refresh", "partner call",
             "analytics dashboard", "survey results", "launch checklist", "press kit", "warehouse audit"]
_VERBS = ["was postponed", "got approved", "needs a second look", "was shared with finance",
          "moved to next sprint", "is waiting on legal", "was archived", "got a new owner"]
_DETAILS = ["after the Tuesday sync", "per the director's note", "until the numbers settle",
            "because the venue changed", "pending the vendor quote", "for the Q3 planning cycle"]


def filler_texts(n: int, seed: int = 7):
    rng = random.Random(seed)
    return [f"Note {i}: the {rng.choice(_SUBJECTS)} {rng.choice(_VERBS)} {rng.choice(_DETAILS)}"
            for i in range(n)]


def make_provider(semantic: bool):
    if not semantic:
        return None
    from cognicore.memory.providers.sentence_transformers import SentenceTransformerProvider
    return SentenceTransformerProvider()


def run(filler: int, provider, data, compact: bool = False) -> dict:
    # The backend's connections close on garbage collection, which Windows may not
    # have done yet when the folder is removed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        backend = SQLiteMemoryBackend(str(Path(tmp) / "bench.db"), provider=provider)
        base = time.time() - 10_000_000  # labeled memories are the oldest rows
        id_map = {}
        labeled = [MemoryEntry(text=m["text"], category="general", timestamp=base + i)
                   for i, m in enumerate(data["memories"])]
        t0 = time.perf_counter()
        for m, stored in zip(data["memories"], backend.store_many(labeled)):
            id_map[stored] = m["id"]
        fill = [MemoryEntry(text=t, category="general", timestamp=base + 1000 + i)
                for i, t in enumerate(filler_texts(filler))]
        for start in range(0, len(fill), 500):
            backend.store_many(fill[start:start + 500])
        save_ms = (time.perf_counter() - t0) * 1000 / (len(labeled) + len(fill))

        backend.search("warm up", top_k=TOP_K)  # first call may load models or build caches
        per_kind, latencies, tokens = {}, [], []
        for q in data["queries"]:
            t = time.perf_counter()
            results = (compact_recall.compact_search(backend, q["q"], top_k=TOP_K) if compact
                       else backend.search(q["q"], top_k=TOP_K))
            latencies.append((time.perf_counter() - t) * 1000)
            tokens.append(sum(len(r.entry.text) for r in results) / 4)
            ranked = [id_map.get(r.entry.entry_id) for r in results]
            rank = ranked.index(q["expect"]) + 1 if q["expect"] in ranked else None
            k = per_kind.setdefault(q["kind"], {"n": 0, "hit1": 0, "r5": 0, "rr": 0.0, "misses": []})
            k["n"] += 1
            k["hit1"] += rank == 1
            k["r5"] += rank is not None
            k["rr"] += 1 / rank if rank else 0.0
            if rank != 1:
                k["misses"].append({"q": q["q"], "rank": rank})

    summary = {}
    for kind, k in per_kind.items():
        summary[kind] = {"hit@1": round(k["hit1"] / k["n"], 3), "R@5": round(k["r5"] / k["n"], 3),
                         "MRR": round(k["rr"] / k["n"], 3), "not_first": k["misses"]}
    latencies.sort()
    return {
        "filler": filler,
        "kinds": summary,
        "tokens_per_search": round(statistics.mean(tokens), 1),
        "save_ms_per_memory": round(save_ms, 2),
        "search_ms_p50": round(statistics.median(latencies), 2),
        "search_ms_p95": round(latencies[int(0.95 * (len(latencies) - 1))], 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--semantic", action="store_true", help="use sentence-transformers embeddings")
    ap.add_argument("--compact", action="store_true", help="use compact recall instead of plain search")
    ap.add_argument("--filler", type=int, nargs="+", default=[0, 6000], help="filler sizes to run")
    ap.add_argument("--out", help="write results as JSON to this file")
    args = ap.parse_args()

    data = json.loads(SET_PATH.read_text(encoding="utf-8"))
    provider = make_provider(args.semantic)
    report = {"mode": ("semantic" if args.semantic else "keyword") + (" + compact" if args.compact else ""), "runs": []}
    for n in args.filler:
        r = run(n, provider, data, args.compact)
        report["runs"].append(r)
        print(f"\n== {report['mode']}, {len(data['memories'])} labeled + {n} filler ==")
        for kind, s in r["kinds"].items():
            print(f"  {kind:<11} hit@1 {s['hit@1']:.3f}   R@5 {s['R@5']:.3f}   MRR {s['MRR']:.3f}")
        print(f"  tokens/search {r['tokens_per_search']}   save {r['save_ms_per_memory']} ms/memory   "
              f"search p50 {r['search_ms_p50']} ms  p95 {r['search_ms_p95']} ms")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()

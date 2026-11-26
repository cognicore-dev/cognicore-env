"""Compare plain search with compact recall on LongMemEval, through the product code.

    python benchmarks/longmemeval/compare_recall.py --dataset oracle|s [--limit N] [--spend 0.5]

For each question its chat history is stored in a fresh keyword-only database (as the hosted
server runs) in windows of 5 turns overlapping by 2, each tagged with its session. Then:

    old = SQLiteMemoryBackend.search(question, top_k=5)          (today's recall)
    new = compact_recall.compact_search(backend, question, top_k=5)

and both are scored the same way:

    recall     every evidence turn of the question is in the returned text
               (the rule in analysis/eval_r5.py, ignoring empty texts)
    tokens     characters / 4 + 12 per result (score, date and id in the recall output)
    context    of the evidence turns found by OLD, the share whose turn 1 or 2 before / after
               is also returned, measured for old and new on those same turns
    speed      time per question for each

It prints the results and PASS / FAIL for the guarantees: new recall >= old, no question
costs more tokens with new, context on old's answers >= old. Datasets: run
benchmarks/data/longmemeval/download_datasets.py first.
"""
import argparse
import json
import logging
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from cognicore.memory import compact_recall as cr  # noqa: E402
from cognicore.memory.base import MemoryEntry  # noqa: E402
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend  # noqa: E402

DATASETS = {
    "oracle": REPO / "benchmarks" / "data" / "longmemeval" / "longmemeval_oracle.json",
    "s": REPO / "benchmarks" / "data" / "longmemeval" / "longmemeval_s_cleaned.json",
}
WINDOW, OVERLAP, TOP_K = 5, 2, 5


def windows(item):
    """(session id, text) for windows of 5 turns overlapping by 2, across the whole history."""
    ids = item.get("haystack_session_ids") or list(range(len(item["haystack_sessions"])))
    turns = [(sid, t) for sid, s in zip(ids, item["haystack_sessions"]) for t in s]
    fmt = lambda w: "\n".join(f"{t['role']}: {t['content']}" for _, t in w)  # noqa: E731
    if not turns:
        return []
    if len(turns) < WINDOW:
        return [(str(turns[0][0]), fmt(turns))]
    return [(str(turns[i][0]), fmt(turns[i:i + WINDOW])) for i in range(0, len(turns) - WINDOW + 1, WINDOW - OVERLAP)]


def found(evidence, texts):
    low = [t.lower() for t in texts if t.strip()]
    return any(evidence in m or (len(evidence) > 20 and evidence[: len(evidence) // 2] in m) for m in low)


def cost(texts):
    return sum(cr.tokens(t) + cr.RESULT_OVERHEAD for t in texts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", choices=DATASETS, default="oracle")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--spend", type=float, help="share of saved tokens extras may use (default: the env setting)")
    ap.add_argument("--out", help="write per-question results as JSON lines")
    args = ap.parse_args()
    logging.disable(logging.WARNING)

    data = json.loads(DATASETS[args.dataset].read_text(encoding="utf-8"))[: args.limit]
    rows = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for n, item in enumerate(data):
            wins = windows(item)
            backend = SQLiteMemoryBackend(str(Path(tmp) / f"q{n}.db"))
            if wins:
                backend.store_many([MemoryEntry(text=t, category="history", session_id=f"s-{sid}",
                                                timestamp=1000.0 + i) for i, (sid, t) in enumerate(wins)])
            q = item["question"]
            t0 = time.perf_counter()
            old = [r.entry.text for r in backend.search(q, top_k=TOP_K)]
            t1 = time.perf_counter()
            new = [r.entry.text for r in cr.compact_search(backend, q, top_k=TOP_K, spend=args.spend)]
            t2 = time.perf_counter()

            convo = [[f"{t['role']}: {t['content']}" for t in s] for s in item["haystack_sessions"]]
            old_blob, new_blob = "\n\0\n".join(old), "\n\0\n".join(new)
            evid = [(si, ti, t["content"].strip().lower()) for si, s in enumerate(item["haystack_sessions"])
                    for ti, t in enumerate(s) if t.get("has_answer") is True]
            ctx = []  # (distance, shown by old, shown by new) for evidence turns old found
            for si, ti, ev in evid:
                if found(ev, old):
                    for d in (-2, -1, 1, 2):
                        if 0 <= ti + d < len(convo[si]):
                            nb = convo[si][ti + d]
                            ctx.append((d, nb in old_blob, nb in new_blob))
            rows.append({
                "question_id": item["question_id"], "type": item["question_type"],
                "old_hit": all(found(ev, old) for _, _, ev in evid),
                "new_hit": all(found(ev, new) for _, _, ev in evid),
                "old_tokens": cost(old), "new_tokens": cost(new),
                "old_ms": (t1 - t0) * 1000, "new_ms": (t2 - t1) * 1000, "ctx": ctx,
            })
            if (n + 1) % 50 == 0:
                print(f"  {n + 1}/{len(data)}", flush=True)

    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    def pct(xs):
        xs = list(xs)
        return 100 * sum(xs) / max(1, len(xs))

    def ms(key, q):
        v = sorted(r[key] for r in rows)
        return v[min(len(v) - 1, int(q * (len(v) - 1)))]

    print(f"\nLongMemEval {args.dataset}, {len(rows)} questions, top_k={TOP_K}, "
          f"extra spend={cr.extra_spend() if args.spend is None else args.spend}\n")
    print("| | old (search) | new (compact recall) | change |\n|---|---|---|---|")
    o, w = pct(r["old_hit"] for r in rows), pct(r["new_hit"] for r in rows)
    print(f"| recall | {o:.1f}% | {w:.1f}% | {w - o:+.1f} pts |")
    ot, nt = statistics.mean(r["old_tokens"] for r in rows), statistics.mean(r["new_tokens"] for r in rows)
    print(f"| tokens per question | {ot:.0f} | {nt:.0f} | {100 * (nt - ot) / ot:+.1f}% |")
    for d, label in ((-2, "2 turns before"), (-1, "1 turn before"), (1, "1 turn after"), (2, "2 turns after")):
        oc = pct(c[1] for r in rows for c in r["ctx"] if c[0] == d)
        nc = pct(c[2] for r in rows for c in r["ctx"] if c[0] == d)
        print(f"| context: {label} (old's answers) | {oc:.1f}% | {nc:.1f}% | {nc - oc:+.1f} pts |")
    print(f"| speed p50 / p95 (ms) | {ms('old_ms', .5):.1f} / {ms('old_ms', .95):.1f} | "
          f"{ms('new_ms', .5):.1f} / {ms('new_ms', .95):.1f} | |")

    by_type = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    print("\n| question type | n | old recall | new recall |\n|---|---|---|---|")
    for t, rs in sorted(by_type.items()):
        print(f"| {t} | {len(rs)} | {pct(r['old_hit'] for r in rs):.1f}% | {pct(r['new_hit'] for r in rs):.1f}% |")

    over = sum(r["new_tokens"] > r["old_tokens"] for r in rows)
    lost = sum(r["old_hit"] and not r["new_hit"] for r in rows)
    ctx_lost = sum(c[1] and not c[2] for r in rows for c in r["ctx"])
    print("\nGuarantees:")
    print(f"  {'PASS' if w >= o else 'FAIL'}  recall new >= old")
    print(f"  {'PASS' if lost == 0 else 'FAIL'}  no question answered by old is missed by new ({lost} missed)")
    print(f"  {'PASS' if over == 0 else 'FAIL'}  no question costs more tokens with new ({over} over)")
    verdict = "PASS" if ctx_lost == 0 else "FAIL"
    print(f"  {verdict}  no context turn shown by old is missing in new ({ctx_lost} missing)")


if __name__ == "__main__":
    main()

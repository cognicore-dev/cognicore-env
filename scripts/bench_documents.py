"""Tokens and answers: pasting a whole document vs recalling it from document memory.

    python scripts/bench_documents.py [--top-k 5] [--download]

For each document in benchmarks/documents/questions.json (fetched into
benchmarks/data/documents/ with --download), the document is added to a fresh memory
database and every question is recalled. Reported per document:

    whole document   tokens of its full extracted text (what pasting it into a chat costs)
    recall           tokens recall returns: section text + 12 per result for score, date, id
    found            questions whose expected text is in what recall returns

Expected texts are checked against the document first; a question whose answer is not in
the document stops the run instead of counting as a miss.
"""
import argparse
import json
import logging
import statistics
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cognicore.memory import documents as docs  # noqa: E402
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend  # noqa: E402

QUESTIONS = REPO / "benchmarks" / "documents" / "questions.json"
DATA = REPO / "benchmarks" / "data" / "documents"
OVERHEAD = 12


def tokens(text: str) -> int:
    return (len(text) + 3) // 4


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--download", action="store_true", help="fetch missing documents from their URLs")
    args = ap.parse_args()
    logging.disable(logging.WARNING)

    spec = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    DATA.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for i, doc in enumerate(spec["documents"]):
            path = DATA / doc["file"]
            if not path.exists():
                if not args.download:
                    sys.exit(f"{path} is missing; run with --download")
                req = urllib.request.Request(doc["url"], headers={"User-Agent": "cognicore-bench"})
                # nosec B310 - fixed https URLs from the checked-in question set
                with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310
                    path.write_bytes(r.read())
            data = path.read_bytes()
            whole = docs._clean("\n".join(t for _, t in docs.read_pages(data, doc["file"])))
            norm = " ".join(whole.split())
            for q in doc["questions"]:
                if q["expect"] not in norm:
                    sys.exit(f"{doc['file']}: expected text {q['expect']!r} is not in the document")

            backend = SQLiteMemoryBackend(str(Path(tmp) / f"d{i}.db"))
            added = docs.add_document(backend, data, doc["file"])
            found, recall_tokens = [], []
            for q in doc["questions"]:
                results = backend.search(q["q"], top_k=args.top_k)
                text = " ".join(" ".join(r.entry.text.split()) for r in results)
                found.append(q["expect"] in text)
                recall_tokens.append(sum(tokens(r.entry.text) + OVERHEAD for r in results))
                if not found[-1]:
                    print(f"  not found: {doc['file']}: {q['q']}")
            rows.append((doc["file"], added.pages, added.sections, tokens(whole),
                         statistics.mean(recall_tokens), sum(found), len(found)))

    print(f"\nDocument memory vs pasting the whole document (top_k={args.top_k})\n")
    print("| document | pages | sections | whole document | recall (avg) | saving | answers found |")
    print("|---|---|---|---|---|---|---|")
    for name, pages, sections, whole_t, recall_t, hit, n in rows:
        print(f"| {name} | {pages} | {sections} | {whole_t:,} | {recall_t:,.0f} | "
              f"{100 * (1 - recall_t / whole_t):.0f}% fewer | {hit}/{n} |")


if __name__ == "__main__":
    main()

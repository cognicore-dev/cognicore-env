"""Reproduce the RME smoke benchmark with python -m benchmarks.history_benchmark."""

import argparse
import gc
import io
import itertools
import json
import statistics
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from cognicore.history import HistoryEngine
from cognicore.history.chatgpt import ChatGPTAdapter
from cognicore.memory.base import MemoryEntry
from cognicore.memory.context_preservation import save_session
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "chatgpt-history.json"
# Reviewed against the fixture by message ID, type, and expected event time.
TRUTH = {
    ("d2", "decision"), ("d3", "failure"), ("d4", "success"), ("d5", "milestone"),
    ("d5", "procedure"), ("d7", "failure"), ("d8", "milestone"), ("d9", "milestone"),
}
QUESTIONS = [
    ("Where did the original tractography discussion start?", "DIPY tractography", "d1"),
    ("Why were memory-mapped arrays selected?", "memory-mapped arrays peak RAM", "d2"),
    ("What happened to batching on Windows?", "vectorized batching Windows", "d3"),
    ("What happened after installing the Linux dependency?", "batching worked Linux", "d4"),
    ("Which command was suggested for the benchmark?", "python dipy_bench", "d5"),
    ("Where are the historical timings?", "DIPY timing table", "d6"),
    ("When did the runtime library cause another failure?", "runtime library differed", "d7"),
    ("What tracing approach did the project switch to?", "sampled traces", "d9"),
]


def measure(search, originals, repeats, k):
    latencies = []
    recalls = dates = sources = messages = 0
    for _question, query, expected in QUESTIONS:
        for _ in range(repeats):
            start = time.perf_counter()
            results = search(query, k)
            latencies.append((time.perf_counter() - start) * 1000)
        found = next((hit for hit in results if (
            expected == hit["message"] or originals[expected]["text"] in hit["text"]
        )), None)
        if found:
            recalls += 1
            dates += found["time"] == originals[expected]["time"]
            sources += found["conversation"] == originals[expected]["conversation"]
            messages += found["message"] == expected
    total = len(QUESTIONS)
    return {
        f"recall_at_{k}": recalls / total,
        "temporal_accuracy": dates / total,
        "conversation_attribution_accuracy": sources / total,
        "message_attribution_accuracy": messages / total,
        "latency_p50_ms": round(statistics.median(latencies), 3),
        "latency_p95_ms": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 3),
    }


def benchmark(root, repeats, k, scale):
    raw = SQLiteMemoryBackend(str(root / "raw.db"))
    conventional = SQLiteMemoryBackend(str(root / "conventional.db"))
    history = HistoryEngine(root / "history.db")
    history.import_archive(FIXTURE, consent=True, project="DIPY")
    originals, session_sources = {}, {}
    source_rows = history.timeline(kind="message")
    refs = {row["message_ref"]: row["original_message_id"] for row in source_rows}
    with FIXTURE.open("rb") as file:
        for source, messages in ChatGPTAdapter().conversations(file):
            original = source["original_conversation_id"]
            transcript = []
            for msg in messages:
                info = {"text": msg["text"], "conversation": original, "time": msg["event_time"],
                        "message": msg["original_message_id"]}
                originals[msg["original_message_id"]] = info
                transcript.append({"role": msg["role"], "content": msg["text"]})
                raw.store(MemoryEntry(text=f"{source['title']}\n{msg['text']}", metadata=info))
            result = json.loads(save_session(conventional, transcript, session_name=source["title"]))
            session_sources[result["session_id"]] = original

    def raw_search(query, limit):
        return [hit.entry.metadata for hit in raw.search(query, top_k=limit) if hit.score > 0]

    def conventional_search(query, limit):
        return [{"text": hit.entry.text, "message": None, "time": hit.entry.timestamp,
                 "conversation": session_sources.get(hit.entry.session_id)}
                for hit in conventional.search(query, top_k=limit) if hit.score > 0]

    def retrospective_search(query, limit):
        return [{"text": hit["text"], "conversation": hit["original_conversation_id"],
                 "message": hit.get("original_message_id") or refs[hit["message_refs"][0]], "time": hit["event_time"]}
                for hit in history.search(query, limit=limit)]

    events = history.timeline()
    extracted = {(refs[item["message_refs"][0]], item["kind"]) for item in events}
    linked = {item["experience_ref"]: refs[item["message_refs"][0]] for item in events}
    conflicts = {
        tuple(sorted((linked[edge["origin"]], linked[edge["target"]])))
        for item in events for edge in item["relations"] if edge["kind"] == "conflict"
    }
    expected_conflicts = {("d4", "d7")}
    version = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scaled = json.dumps([
        {**conversation, "conversation_id": f"{conversation['conversation_id']}-{index}"}
        for index, conversation in itertools.product(range(scale), version)
    ]).encode()
    throughput = HistoryEngine(root / "throughput.db")
    start = time.perf_counter()
    imported = throughput.import_stream(io.BytesIO(scaled), consent=True)
    elapsed = time.perf_counter() - start
    before = throughput.backend.count()
    duplicate = throughput.import_stream(io.BytesIO(scaled), consent=True)
    return {
        "fixture": {"questions": len(QUESTIONS), "messages": len(originals), "ground_truth_claims": len(TRUTH)},
        "raw_bm25": measure(raw_search, originals, repeats, k),
        "conventional_session_memory": measure(conventional_search, originals, repeats, k),
        "retrospective_retrieval": measure(retrospective_search, originals, repeats, k),
        "extraction": {"precision": len(TRUTH & extracted) / max(1, len(extracted)),
                       "recall": len(TRUTH & extracted) / len(TRUTH)},
        "contradiction_detection": {"precision": len(conflicts & expected_conflicts) / max(1, len(conflicts)),
                                    "recall": len(conflicts & expected_conflicts) / len(expected_conflicts)},
        "import": {"conversations": imported["conversations"], "messages": imported["messages"],
                   "archive_bytes": len(scaled), "elapsed_seconds": round(elapsed, 3),
                   "messages_per_second": round(imported["messages"] / elapsed, 1),
                   "duplicate_index_entries_added": throughput.backend.count() - before,
                   "duplicate_batch_reused": duplicate["id"] == imported["id"]},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=25)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--scale", type=int, default=100)
    args = parser.parse_args()
    if min(args.repeats, args.k, args.scale) < 1 or args.k > 200:
        parser.error("Use positive arguments with --k no greater than 200.")
    with TemporaryDirectory() as path:
        try:
            results = benchmark(Path(path), args.repeats, args.k, args.scale)
        finally:
            gc.collect()  # The conventional SQLite backend leaves cyclic connections on Windows.
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

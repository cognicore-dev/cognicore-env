# Benchmarks

## Retrospective Memory Engine

Run `python -m benchmarks.history_benchmark` from the checkout. The [synthetic ChatGPT archive](../examples/chatgpt-history.json) has three conversations and nine messages. The benchmark includes eight manually checked retrieval questions, eight labeled claims, and one labeled candidate conflict. Each query is timed 25 times. The throughput run imports 100 copies of the three conversations with distinct conversation IDs, then repeats the exact archive to check deduplication.

Raw search indexes each source message and uses the same SQLite BM25 backend. The conventional baseline uses the existing deterministic session save and recall pipeline, and the benchmark maps its sessions back to conversation IDs. RME searches both indexed source messages and claims. Recall@5 counts questions with a matching source or verbatim source text in the top five. Temporal and attribution accuracy use all questions as the denominator, including misses. A null timestamp is correct for the undated attachment record.

| Retrieval method | Recall@5 | Temporal accuracy | Conversation attribution | Message attribution | p50 / p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Raw BM25 | 100% | 100% | 100% | 100% | 1.035 / 1.179 ms |
| Conventional session memory | 50% | 0% | 50% | 0% | 1.051 / 1.376 ms |
| Retrospective retrieval | 100% | 100% | 100% | 100% | 1.927 / 2.082 ms |

On Windows with Python 3.11.9 and SQLite 3.45.1, RME extracted seven of eight labeled claims with 100% precision and 87.5% recall. It found the single labeled candidate conflict. The throughput run imported 900 messages in 3.675 seconds (244.9 messages per second) and added zero index entries on exact reimport. This small fixture checks behavior reproducibly. It cannot establish a recall advantage over raw search or characterize extraction quality on real exports.

## LongMemEval

LongMemEval tests how well an agent can recall facts that are scattered across many past conversations — not just recent ones. It's the hardest memory benchmark because the answer requires **combining evidence from multiple separate sessions**.

### How CogniCore solves it — Multi-Hop Adapter

Most retrieval systems grab the top-N most similar chunks and stop. That fails when the answer is split across chunks that don't individually look relevant.

CogniCore's **Multi-Hop Adapter** works differently:

1. **Extract targets** — pull key names and entities from the query
2. **Hop-1 retrieval** — find the most relevant anchor chunks
3. **Graph traversal** — follow session-ID and time links to find connected chunks the first hop missed
4. **Coverage selection** — pick the set of chunks that together cover the most entities — not just the highest individual scores

### Results (STRICT R@5)

| Context window | Baseline (ZeroShot) | CogniCore Multi-Hop | Gain |
|:---:|:---:|:---:|:---:|
| **5 chunks** | 78.8% | **85.2%** | **+6.4%** 🚀 |
| **10 chunks** | 87.2% | **92.8%** | **+5.6%** 🚀 |
| **20 chunks** | 95.0% | 95.0% | — (brute force catches up) |

At small window sizes — where token efficiency matters — the Multi-Hop Adapter clearly wins by reconstructing dispersed evidence instead of hoping it all fits in one chunk.

Run the benchmark yourself:

```bash
python benchmarks/longmemeval/runner.py
```

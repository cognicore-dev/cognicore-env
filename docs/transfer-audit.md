# Memory transfer audit

Status: all 10 improvements implemented on branch `feat/memory-transfer`. Results are in
[Before vs after](#before-vs-after) at the end. Date: 2026-10-27.

Scope: `cognicore_save_session`, `cognicore_resume_session`, `cognicore_compress_context`
and the experience tools (`record`, `recall`, `check`, `share`, `verify`), as served by
both `cognicore/mcp/server.py` (local stdio) and `cognicore/connectors/remote.py` (hosted).

## How it works today

Both servers are thin wrappers. All session logic is in
[`cognicore/memory/context_preservation.py`](../cognicore/memory/context_preservation.py),
storing into `SQLiteMemoryBackend`.

| Tool | What it stores | Format |
|---|---|---|
| `save_session` | Every message is run through `decompose()` and stored as separate `memory_entries` rows. Sentences matching decision or TODO keywords are stored a second time as `decisions` / `action_items`. Code blocks go to `code_snippets`. A `session_summary` row holds the **first** decision and **first** action only. A `sessions` row holds name, timestamp, message count and summary id. | Free text rows. No schema version, no provenance. |
| `resume_session` | Reads the latest `sessions` row's summary, then `search(query or "", top_k=5)` as "Key facts", 5 `action_items`, 3 `decisions`. | Plain-text brief. |
| `compress_context` | Keyword-classified sentences (numbers, decisions, problems, next steps, ideas) plus up to 2 code blocks, capped at 40% of input. Stored as one `conversation_summary` row. | Plain-text summary inside JSON. |
| Experience tools | `StructuredExperience` serialized into `MemoryEntry.metadata["experience"]`: task, attempts, solution, evidence, `RepositoryContext`, `EnvironmentContext` (python, os, framework, deps). | Structured and hashed, but separate from sessions. |

## Method

A script ran the real functions against 3 sample sessions, each with a set of planted
facts (for example `AUTH-1423`, `PyJWT 2.8.0`, `#111111`, `0042_rename_user_id.sql`,
`42P01`). It also ran one long mixed session (105 messages). Each sample includes
chatter ("Hey!", "Sounds good") and, in session 3, a fake API key.

- **Tokens** use the repo's own estimator (`chars / 4`).
- **Facts recovered** is the percentage of planted values that appear verbatim in the resume brief.
- **Findable via recall** is the percentage of planted values returned by `search()` (top 5) when queried directly. In other words: is the value stored somewhere retrievable at all?
- **Latency** is the median of 5 runs on a local SQLite file (Windows 11, Python 3.12).
- **Search mode** is BM25 only. `sentence-transformers` is not installed in the venv, so the hosted server's semantic mode was not measured.

The script will become `scripts/bench_transfer.py` in Step 3.

## Results (baseline)

| Session | Input tok | Rows stored | Stored tok | Save ms | Resume tok | Resume ms | Facts recovered (no query / with query) | Findable via recall |
|---|---|---|---|---|---|---|---|---|
| s1 JWT debug (11 facts) | 165 | 21 | 265 | 185 | 184 | 5.7 | **45% / 45%** | 73% |
| s2 launch plan (9 facts) | 125 | 21 | 218 | 174 | 166 | 5.5 | **56% / 33%** | 89% |
| s3 DB migration (9 facts) | 166 | 18 | 271 | 151 | 169 | 6.2 | **56% / 33%** | 67% |
| long mixed (29 facts, 105 msgs) | 1,860 | 181 | – | 1,441 | 267 | – | **34%** | – |

Cross-session handoff (s1, s2 and s3 saved in order, then resuming each topic):

| Resume target | Facts recovered | Tokens |
|---|---|---|
| s1 with query | 45% | 332 |
| s2 with query | 33% | 274 |
| s3 with query | 33% | 293 |
| s1 with no query (the "open Cursor and continue" case) | **27%** | 265 |

`compress_context` (default `keep_last_n=5`):

| Session | Ratio | Facts kept (summary + last 5 msgs) | Older facts kept in summary |
|---|---|---|---|
| s1 | 1x | 82% | 67% |
| s2 | 1x | 89% | 67% |
| s3 | 1x (output 197 tok > input 166) | 100% | 100% |
| long | 5x (1,860 → 410) | 76% | – |

What was lost on resume: commit hashes, versions (`3.11.4`, `0.110.0`, `20.11.1`),
file paths, root cause, rejected approaches, brand color, price, domain, error codes, env var
names and code. These are mostly exact values, which are exactly what a new agent needs
to continue without re-asking.

Sample brief for s1 (abridged). Note that 3 of the 5 "Key facts" are chatter:

```
Key facts:
  - Hey!
  - Can you help me with something?
  - what are you working on?
  - Our goal is to fix intermittent 401s on /api/v2/orders
  - Ticket is AUTH-1423
```

## Findings

### Correctness bugs

1. **Sessions saved in the same second overwrite each other.** `session_id = f"sess_{%Y%m%d_%H%M%S}"`
   ([context_preservation.py:381](../cognicore/memory/context_preservation.py#L381)) combined with
   `INSERT OR REPLACE` ([:493](../cognicore/memory/context_preservation.py#L493)). Measured: 2 saves
   produced 1 `sessions` row, with both sessions' memories under one id. Two agents saving
   at the end of a turn is the normal case for cross-agent use.
2. **`keep_last_n=0` compresses nothing.** `conversation[:-0]` is `[]`
   ([:179-181](../cognicore/memory/context_preservation.py#L179-L181)). Measured: 0 messages compressed.
3. **`last_n_sessions` is effectively ignored.** Resume fetches N sessions but only prints
   `session_rows[0]` ([:541-556](../cognicore/memory/context_preservation.py#L541-L556)).
4. **The "Key facts" ordering is arbitrary.** `search("")` orders by `timestamp`, but every row
   in a session shares one timestamp, so the first-inserted rows win. Those are usually greetings.
5. **Wrong fallback goal.** If no status sentence is found, compress writes "Active development on
   CogniCore MCP server and memory systems." ([:276](../cognicore/memory/context_preservation.py#L276))
   into every user's summary.
6. **Compress can make output larger than input.** The 500-character floor ([:324](../cognicore/memory/context_preservation.py#L324))
   pushes small inputs above their original size, and `compression_ratio` still reports "1x".

### Size and noise

7. **Duplicates.** Decisions and TODOs are stored twice (as a fact and again as a
   decision or action). Re-saving the same conversation doubles the rows (21 → 42), because
   nothing is deduplicated or idempotent. In the long session, 125 of 181 rows (69%) were duplicates.
8. **Chatter is stored and resumed** (2–4 rows per short session, 10 in the long one).
9. **The summary keeps only the first decision and first action** ([:471-474](../cognicore/memory/context_preservation.py#L471-L474)).
   Later decisions are reachable only by search.

### Missing for cross-agent handoff

10. **No structured handoff.** The resume output is unversioned prose. There are no goal,
    current-state, open-task or files-touched sections, and there is no way to say "give me the rest".
11. **No provenance.** `source_agent` is empty on all saved rows (0 of 60), and the `sessions` table has no
    agent or client column. Timestamps are local time without a timezone
    (`datetime.fromtimestamp`), which is ambiguous across machines.
12. **No token budget.** Resume size is fixed by item counts, not by the caller's budget.
13. **Resume does not consult experiences**, and sessions don't link to them.
14. **Environment fingerprint is partial and manual.** `EnvironmentContext` has `os`, but
    `cognicore_record_experience` exposes no `os` parameter ([remote.py:949](../cognicore/connectors/remote.py#L949)),
    and nothing captures the environment automatically. Compatibility checks run only when the
    caller passes the current env to `recall` or `check`, never on resume. On the hosted server
    the server's own environment is irrelevant, so the client must supply the fingerprint.

### Safety and performance

15. **Secrets are stored verbatim.** The fake `sk-test-…` key in s3 was saved as a memory row and
    would be shown on resume or shared. This applies to `save_session` and `compress_context` alike.
16. **Saves are slow.** Every row opens its own connection and commits separately (`SQLiteMemoryBackend.store`).
    That is about 8 ms per row, so the 105-message session took 1.4 s. With an embedding provider,
    each row also costs an embed call.

## Ranked improvements

Ranked by expected gain in facts recovered per unit of effort, with correctness first.

| # | Change | Fixes | Expected effect |
|---|---|---|---|
| 1 | Fix the session-id collision (UUID-based id), `keep_last_n=0`, `last_n_sessions`, and the hardcoded fallback goal | 1, 2, 3, 5 | No silent data loss between agents |
| 2 | **Versioned handoff record** (`cognicore.handoff/v1`, JSON Schema in `docs/`) written by `save_session`: `goal`, `current_state`, `decisions[{what, why}]`, `open_tasks[{task, status}]`, `key_facts`, `files`, `provenance{agent, client, session_id, created_at (UTC ISO-8601)}`, `environment`. Stored in a new `sessions.handoff_json` column, using the existing ALTER-migration pattern. | 9, 10, 11 | Main driver of facts recovered |
| 3 | **Verbatim exact-value extraction** into `key_facts`: paths, versions, ids and tickets, hashes, error codes, env var names, URLs and domains, money, dates and numbers, with the source sentence kept | lost-value list | Targets the facts that were missed |
| 4 | **Token-budgeted resume**: optional `max_tokens` and `format` ("text" or "json") parameters. Items are filled in priority order: goal, open tasks, recent decisions, key facts, files, other. A `more` list of ids is returned for on-demand retrieval (for example `cognicore_resume_session(session_id=…, section=…)`, using only new optional parameters). | 4, 12 | Predictable size; nothing important cut first |
| 5 | **Dedupe and chatter filter on save**: store each sentence once with a type tag, skip greetings, acknowledgements and bare questions, and make re-saves idempotent by content hash | 7, 8 | Roughly 50–70% fewer rows on long sessions |
| 6 | **Secret detection and redaction** on save and compress (API key prefixes, JWTs, PEM blocks, `password=`, high-entropy tokens). Redact rather than refuse, so the rest of the session still saves, and report the count. | 15 | No secrets stored |
| 7 | **Batch writes** in one transaction (`store_many`), plus batched embedding when a provider exists | 16 | Save time down about 10x |
| 8 | **Environment fingerprint**: add optional `os` and an `environment_json` parameter to `record_experience` and `save_session`. Resume accepts an optional current environment and emits `environment_warnings` using the existing `EnvironmentChecker`. Resume also lists relevant verified experiences under the budget. | 13, 14 | Warns when Cursor-on-Windows resumes Claude-on-macOS work |
| 9 | **Backward compatibility**: sessions without `handoff_json` are upgraded on read. A v1 handoff is built from their existing rows, marked `"migrated": true`, and the old text brief stays the default output. | – | Old sessions still resume |
| 10 | Compress fixes: never output more than the input, and report the real ratio | 6 | Accurate metrics |

Step 3 targets: facts recovered (no query, after 3 sessions) from **27% to ≥ 85%**, the long-session
resume within a 500-token budget at ≥ 80%, duplicate rows below 5%, and the long-session
save below 200 ms (BM25 mode).

## Decisions needed before Step 2

1. **Where the handoff lives.** I recommend a new nullable `sessions.handoff_json` column
   rather than a new table or memory row: it migrates cheaply, and old readers ignore it.
2. **Default resume output.** I recommend keeping the text brief as the default (for compatibility) and adding
   `format="json"` for the structured handoff. The text brief would itself be rendered
   from the handoff, so it gets better too.
3. **Secrets: redact or refuse.** For `save_session` I recommend redaction, so one pasted
   key doesn't lose the whole session. (Prompt 2 covers refusal on `cognicore_remember`.)
4. **Semantic mode.** Should the Step 3 benchmark also install `sentence-transformers` to measure
   the hosted configuration? It's a large download (torch).

## Before vs after

Measured with `python scripts/bench_transfer.py`, run on upstream `main` (before, via `--repo`)
and on this branch (after). BM25 only, Windows 11, Python 3.12, median of 5 runs.

**Read these numbers with one caveat.** The extraction rules were tuned on the three *dev*
sessions (s1–s3). The three *held-out* sessions (h1 front-end bug, h2 training run, h3 prod
incident) were written afterwards in a terser chat style and were not used for tuning.
All six are short, so at the default 1,500-token budget almost nothing is cut. The budgeted
rows and the 105-message session are the more demanding tests.

### Per session (saved alone, then resumed)

| Session | Save ms | Rows | Dupes | Chatter | Resume tok | Facts recovered (no query) | With query | At 150 tok | At 300 tok |
|---|---|---|---|---|---|---|---|---|---|
| s1 JWT debug (dev) | 207 → **21** | 21 → 10 | 1 → 0 | 3 → 0 | 184 → 222 | 45% → **100%** | 45% → **100%** | 36% | 100% |
| s2 launch plan (dev) | 219 → **19** | 21 → 9 | 1 → 0 | 4 → 0 | 166 → 166 | 56% → **100%** | 33% → **100%** | 44% | 100% |
| s3 DB migration (dev) | 239 → **26** | 18 → 11 | 2 → 0 | 2 → 0 | 169 → 234 | 56% → **100%** | 33% → **100%** | 33% | 100% |
| h1 front-end bug (held-out) | 203 → **28** | 16 → 14 | 1 → 0 | 1 → 1 | 127 → 241 | 30% → **100%** | 0% → **100%** | 30% | 100% |
| h2 training run (held-out) | 110 → **22** | 12 → 11 | 0 → 0 | 0 → 0 | 99 → 168 | 67% → **100%** | 22% → **100%** | 56% | 100% |
| h3 prod incident (held-out) | 93 → **28** | 11 → 8 | 0 → 0 | 1 → 0 | 94 → 151 | 50% → **100%** | 25% → **100%** | 67% | 100% |

### Long session (105 messages, all three dev topics, filler and restatements)

| | Before | After |
|---|---|---|
| Save time | 1,231 ms | **36 ms** |
| Rows stored / duplicates / chatter | 181 / 125 / 10 | **27 / 0 / 0** |
| Stored tokens | 1,915 | 520 |
| Resume tokens (default) | 267 | 501 |
| Facts recovered (default) | 34.5% | **100%** |
| Facts recovered with `max_tokens=500` | – | **96.6%** (492 tokens) |

### Cross-agent handoff (three agents save into one memory, a fourth resumes)

| | Dev before → after | Held-out before → after |
|---|---|---|
| Resume each topic by query | 33–45% → **100%** | 0–25% → **100%** |
| Latest session, no query | 56% → **100%** | 50% → **100%** |
| Oldest session, no query (only as an "other session") | 27% → **82%** | 0% → **90%** |
| Resume tokens | 265–332 → 430–504 | 71–117 → 430–490 |

### Correctness and safety

| Check | Before | After |
|---|---|---|
| Two saves in the same second | 1 session row when the saves share a second (timing dependent) | always 2 (unit test) |
| Re-saving the same conversation | rows doubled (21 → 42) | no-op (9 → 9) |
| Fake API key in the conversation | stored verbatim | `[REDACTED:api_key]` in rows, handoff and compress |
| `compress_context(keep_last_n=0)` | 0 messages compressed | all messages |
| Rows with `source_agent` | 0 | all rows saved with `agent=` |
| Environment change on resume | not detected | warnings, e.g. OS mismatch, `BLOCKER: pyjwt 2.x vs 1.x` |
| Pre-v1 sessions | – | converted on read (marked `migrated`) and covered by a test |

### Targets set in this audit

| Target | Result |
|---|---|
| Facts recovered for an older session after 3 saves, no query: 27% → ≥ 85% | **Missed on dev (82%)**, met on held-out (90%). A no-query resume focuses on the latest session; older sessions get goal, state, open tasks, 3 decisions and 5 key facts each. Passing `query` (or `session_id`) gets 100%. |
| Long session at a 500-token budget ≥ 80% | Met: 96.6% |
| Duplicate rows below 5% | Met: 0% |
| Long-session save below 200 ms (BM25) | Met: 36 ms |

Tests: `tests/test_transfer.py` and `tests/test_transfer_tools.py` (46 new) cover the format and schema, pre-v1
migration, budget truncation and floor, dedupe, idempotent re-save, redaction, environment warnings,
on-demand sections, JSON output and MCP parameter compatibility. `tests/test_mcp_server.py` adds a
save in one stdio server process and a resume in a fresh one. Full suite: 1057 passed, 10 skipped.
The same 11 `tests/test_content_studio.py` failures (ModuleNotFoundError) occur on upstream `main`.

## Trade-offs and known limits

- **Briefs are bigger.** The default resume grew from about 170–330 tokens to about 150–500 because it now
  carries the goal, state, decisions, facts, files and older sessions. Tokens per recovered fact
  fell (s1: 37 → 20). Agents that need a small brief should pass `max_tokens`. At 150 tokens it
  keeps the goal, state and open tasks, and recovers 30–67% of facts.
- **Rule-based extraction.** No LLM calls, so it is fast and deterministic, but phrasing outside the
  patterns is classified as a plain key fact rather than as a decision or task. It is still kept
  verbatim, just ranked lower. Scores come from 6 short sessions plus one long one. A larger,
  more varied set (prompt 2's eval harness) is the right next check.
- **Verbatim score.** "Facts recovered" checks that the exact value is present in the brief,
  not that a model answers correctly from it.
- **Redaction errs towards keeping text.** Hex hashes, versions and paths are deliberately not
  redacted. A secret in an unusual format, with no known prefix and low entropy, can slip through.
- **Environment is client-supplied** on the hosted server. The local stdio server fills in the OS only.
- **Compress on tiny inputs** still expands slightly (0.7–0.9x, now reported honestly with
  `expanded: true`), because the section headers cost more than a few sentences.
- **Not changed here:** `SQLiteMemoryBackend.search` logs `FTS5 search failed` for queries with
  `-`, `/` or `.` (e.g. `payments-api`) and falls back. That is retrieval behaviour, so it is left
  for prompt 2's eval-driven work.
- Semantic (sentence-transformers) mode was not benchmarked. `--semantic` is supported once it is installed.

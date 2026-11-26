# Retrospective Memory Engine

CogniCore can import a user's historical ChatGPT conversations, index the original messages, and reconstruct decisions, attempts, outcomes, milestones, and procedures. Every extracted claim points to the source message and uses its original event time.

## Import and recall an old conversation

Download the archive through ChatGPT's data export controls or use the synthetic archive included in this repository. [OpenAI's export instructions](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data)

From a checkout with the package installed, run the following commands. To use your export, substitute its ZIP or conversation JSON file for the sample path and use your own project label.

~~~sh
cognicore history import examples/chatgpt-history.json --consent --project DIPY
cognicore history search "memory-mapped arrays" --project DIPY --kind decision
cognicore history timeline --project DIPY --kind decision
cognicore history timeline "DIPY tractography" --project DIPY --kind message --earliest
cognicore history inspect dipy-architecture-2025
cognicore history reconstruct "vectorized batching" --project DIPY
~~~

The decision result describes choosing memory-mapped arrays over copying volumes due to peak RAM. Its original timestamp is March 3, 2025 at 09:01 UTC (Unix time 1740992460). It comes from message d2 in conversation dipy-architecture-2025 and is classified HISTORICAL_UNVERIFIED. The earliest matching tractography message in the sample is d1 at 09:00 UTC the same day. Reconstruction follows the reported Windows failure, later Linux success, and subsequent Linux failure across three conversations.

## Architecture assessment and design

The local memory hooks save handoffs in SQLite, index them for ordinary recall, and restore them at SessionStart. Sync pushes versioned handoffs from the sessions table and pulls them into normal session memory. Reflection, failure warnings, and verified experience recall consume live memories under their existing lifecycle rules. The Claude Code experience plugin collects Bash evidence via PostToolUse and exposes record, verify, recall, check, and share through the experience manager.

RME reuses CogniCore's SQLite MemoryEntry, FTS5/BM25, optional hybrid embedding search, Attempt, EvidenceRecord, and the structural VerificationGate. It opens a separate instance of that backend under COGNICORE_DATA_DIR/history/history.db (default ~/.cognicore/mcp/history/history.db). It adds provenance tables beside the standard memory and FTS tables. The separate file keeps historical archives outside ordinary recall, automatic briefs, session handoff sync, experience promotion, sharing, and memory event subscribers. History has a separate opt-in sync protocol.

| Record | Stored data |
| --- | --- |
| Batch | Opaque batch ID, SHA-256 of archive bytes, adapter, import time, extraction settings, checkpoint, and status |
| Source | Stable platform-scoped reference, original ID if supplied, title, original creation timestamp, project, attachment references, and parsing status |
| Message | Versioned reference, original ID, mapping node and parent, role, speaker, recipient, timestamp, content, attachment references, and available tool fields |
| Extracted experience | Source message reference, quoted excerpt, fields captured by extraction rules, and evidence and relation history |
| Relation | Links between historical experiences for related approaches, candidate conflicts, or reviewed supersession |
| Execution receipt | Command description, exit code, output SHA-256, execution time, commit if supplied, and local operator's source attribution |
| Audit event | Timestamp, action, and opaque reference for imports, evidence, links, and deletions |

Each stored message and conversation carries its first import batch and time, a digest of the adapted original data, and a digest of the stored representation. Inspect reports stored-content integrity separately from the evidence classification. A digest detects changes against the stored digest and provides no authentication of the export or its author.

Messages use their own event time. Unknown timestamps remain null and sort after known dates. A checkpoint becomes durable with each committed conversation. Retry the same bytes with the same settings to resume; the importer reads the stream again to reach the checkpoint. An interrupted batch can contain searchable conversations already committed. Archive and message deduplication also preserve membership across overlapping exports. Source metadata and project retain their first-seen values; changed versions of messages remain searchable. Each source is assigned a single optional project label at import.

The adapter supplies a platform name and yields conversation metadata plus messages from a seekable binary stream. Another Python adapter can supply a different source format. A custom Python extractor can emit the same claim contract; the default rules are local and deterministic. Pass an existing local embedding provider to HistoryEngine for hybrid search. Default CLI, REST, and MCP search use lexical BM25. Reconstruction follows the stored relations from matching claims and returns the events chronologically.

## ChatGPT format

The adapter reads UTF-8 conversation objects, arrays, and objects containing a conversations array. A ZIP may contain conversations.json or split files such as conversations-001.json and conversations_001.json. Conversation arrays stream one record at a time. A wrapper object is decoded as one record. The importer reads selected ZIP members in place and ignores unrelated account export files.

Mapping-based exports preserve all branches and the recorded parent links. A messages list is supported as an alternate representation. Unusable conversation objects are counted as skipped. Incomplete messages remain inspectable when a source can be identified. Invalid JSON syntax interrupts the batch. Creation times accept Unix seconds and timezone-qualified ISO timestamps. Attachment and tool references remain data; no binary attachment content is extracted or fetched. The CLI accepts up to 4 GiB of archive bytes and selected uncompressed ZIP data. Each decoded JSON record is limited to approximately 32 MiB. REST applies a 128 MiB upload limit.

## Evidence and history

| Classification | Meaning |
| --- | --- |
| HISTORICAL_UNVERIFIED | An attributed archival statement or extraction. This is the initial classification, including for assistants saying tests passed. |
| HISTORICAL_EVIDENCE_ATTACHED | A dated execution receipt with an identified source was attached by a trusted local operator. |
| REVERIFIED | A trusted local operator attested to relevant execution after import and the receipt recorded a successful exit. |
| CONFLICTED | Opposing historical claims are linked and remain available for review. |
| SUPERSEDED | A reviewed supersession links this claim to a newer one. Both claims remain inspectable. |

The default extractor links opposing success and failure claims with the same normalized approach in a project, or in a single conversation when there is no project. Matching environment labels yield a candidate conflict. Different labels yield a related approach. Missing environment details, ambiguous subjects, and branch alternatives can affect this heuristic.

Use cognicore history link ORIGIN TARGET conflict for a reviewed contradiction or cognicore history link NEWER OLDER supersedes to record supersession. The experience command shows the source references, current classification, related claim IDs, and the retained evidence receipts. When classifications overlap, supersession takes precedence over conflict, then reverification and attached evidence.

A trusted operator can use the evidence command with --command, --exit-code, --output-file, --timestamp, and --source. Supply --reverify for relevant new execution after import. CogniCore hashes the supplied output bytes and uses the existing gate to validate the successful receipt. The operator establishes that the receipt came from the named source and addresses the claim. RME provides no cryptographic attestation of the executor and never launches the recorded command.

## CLI reference

Run cognicore history --help or any subcommand with --help for its arguments.

| Subcommand | Function |
| --- | --- |
| import PATH --consent | Import or resume with optional --project. --no-redact is an explicit local override. |
| status [BATCH] | List batches or inspect a checkpoint, counts, and status. |
| search QUERY | Search matching messages and claims. Optional --project, --kind, and --limit. |
| timeline [QUERY] | List matching claims by event time. Use --kind message for source messages and --earliest for the earliest dated match. |
| reconstruct QUERY | Search extracted claims and follow their relationships across sources. |
| inspect CONVERSATION | Inspect source content by original conversation ID or opaque reference. Use --message, --limit, and --offset for detail. |
| experience REF | Inspect an extracted claim and its evidence and provenance. |
| link ORIGIN TARGET KIND | Attach a reviewed relationship. |
| evidence REF | Attach an execution receipt from an independently observed run. |
| delete --batch ID | Remove an import and any records unique to it. |
| delete --conversation REF | Remove the conversation, all of its message versions, claims, evidence, relations, and search entries. |
| audit | List recent history changes with timestamps and opaque references. |
| sync | Enable, check, pause, or run automatic history synchronization. |

## Sync history across devices

Enable history sync once, on the device holding the archive. This authorizes the initial upload to your CogniCore account.

~~~sh
cognicore login
cognicore history sync enable --consent
cognicore history sync status
~~~

Enabling sync immediately reconciles all stored historical conversations, messages, attachment references, claims, evidence, links, import metadata, and deletions with the signed-in account. This includes content previously imported with `--no-redact`. Regular login and session sync leave archive uploads disabled until this separate consent is given. History sync is bound to the server and signed-in account. Switching accounts requires a separate local data directory.

On another device, sign in to the same account. CogniCore restores history automatically into an empty local archive. Discovery also runs during agent activity, so a device already signed in can pick up the archive. A device with its own unlinked history needs explicit consent to upload and merge that data.

After the device links, history changes and agent activity trigger synchronization in the background. `cognicore history sync now` runs it immediately. `cognicore history sync disable` pauses that device while retaining its local copy and the hosted copy. The existing `COGNICORE_SYNC=0` pause applies to history too.

The server uses an authenticated `GET` and `PUT` at `/sync/history`. A revision check prevents stale overwrites, and clients merge independent changes. Source and batch deletion markers prevent an older device from restoring deleted data. Hosted sync stores a compressed logical archive separate from ordinary session memory. The current protocol transfers up to 32 MiB compressed and 256 MiB before compression per account snapshot. The account server must run a release that includes the history sync endpoint.

## Local MCP example

Use the following local stdio server configuration with a client you trust to read the archive. The client and CLI must resolve the same COGNICORE_DATA_DIR. Add its absolute value to env if you use a custom directory.

~~~json
{
  "mcpServers": {
    "cognicore-history": {
      "command": "cognicore",
      "args": ["mcp", "serve", "--transport", "stdio"],
      "env": {
        "COGNICORE_ENABLE_HISTORY_MCP": "1",
        "COGNICORE_TOOLSETS": "history"
      }
    }
  }
}
~~~

After importing the sample, call cognicore_search_history with {"query":"memory-mapped arrays","project":"DIPY","kind":"decision"}. Pass the returned conversation_ref and first message_refs value as conversation and message to cognicore_inspect_historical_source. The source response contains message d2, its original text and role, and its import provenance. Ask, “Why did I choose memory-mapped arrays in March 2025? Show the original message.”

| Tool | Main inputs |
| --- | --- |
| cognicore_search_history | query, project, kind, limit |
| cognicore_recall_timeline | project, kind, query, earliest, limit |
| cognicore_reconstruct_experience | query, project, limit |
| cognicore_inspect_historical_source | conversation, message, limit, offset |
| cognicore_inspect_historical_experience | ref |

Search and timeline return message excerpts. Use source inspection for retained original content. Archive import and mutation are handled by the CLI and authorized REST methods. Hosted MCP and the CLI's SSE transport have no history tools.

## REST API

Set COGNICORE_HISTORY_API_TOKEN to a randomly generated token of at least 32 characters in the environment of an operator-controlled CogniCore REST server. For example, generate one with python -c "import secrets; print(secrets.token_urlsafe(32))". Start the app with uvicorn cognicore.server:create_app --factory --host 127.0.0.1. Include Authorization: Bearer TOKEN in each request. An unset token disables the endpoints.

~~~sh
curl --fail-with-body -H "Authorization: Bearer $COGNICORE_HISTORY_API_TOKEN" \
  -H "Content-Type: application/json" --data-binary @examples/chatgpt-history.json \
  'http://127.0.0.1:8000/history/imports?consent=true&project=DIPY'

curl --fail-with-body -H "Authorization: Bearer $COGNICORE_HISTORY_API_TOKEN" \
  --get 'http://127.0.0.1:8000/history/search' \
  --data-urlencode 'query=memory-mapped arrays' --data-urlencode 'kind=decision'
~~~

| Method and route | Behavior |
| --- | --- |
| POST /history/imports | Upload ZIP or JSON bytes with consent=true. Optional project query parameter. |
| GET /history/imports and /history/imports/{batch} | Check import state, checkpoint, and counts. |
| GET /history/search | Retrieve role-attributed excerpts and claims with scores, dates, and evidence classifications. |
| GET /history/timeline | List chronological events with optional project, kind, query, and earliest filters. |
| GET /history/reconstruct | Retrieve related claims with their sources. |
| GET /history/sources/{conversation} | Inspect source data with optional message, limit, and offset. |
| GET /history/experiences/{ref} | Inspect a claim and its relations and evidence. |
| GET /history/audit | Read the mutation audit trail. |
| DELETE /history/imports/{batch} | Delete a batch. Shared messages and sources survive while referenced by another batch. |
| DELETE /history/sources/{conversation} | Delete a source across all current batches. |

## Privacy and limits

Authorization is single-user. CLI access is governed by the local OS account and data directory permissions. REST uses an opt-in bearer token. Local stdio access grants the configured agent read access to the archive. The client decides where retrieved data goes after a tool call, including whether it sends the result to a model provider or quotes it into a new session.

Pattern-based credentials redaction runs before original content or extracted claims are stored or indexed. The detector can miss secrets, personal data, or proprietary material. The default SQLite file is plaintext, and access depends on the data directory and operating system permissions. The opt-in hosted copy is compressed and stored within your account. Hosted MCP and marketplace sharing do not expose the archive. Imported instructions and commands are treated as quoted data.

Deletion removes source records and derived database and FTS entries. Audit metadata remains. Original exports, copied results, client caches, backups, and filesystem snapshots are outside the archive. SQLite index pages and storage media may also retain recoverable bytes. During an interrupted import, source deletion prevents that source from reappearing when the batch is resumed. With hosted sync enabled, deletion markers continue to apply to reimports of the same source or batch. Reimporting overlapping data preserves the existing record and its first import's redaction treatment.

The current adapter covers the documented ChatGPT inputs. Extraction rules can miss implicit decisions, nuanced contradictions, or important facts expressed outside covered templates. Default search ranks lexical matches. Dense retrieval requires a supplied local embedding provider. The API and CLI run extraction during ingestion with durable progress between conversations. [Benchmark method and measurements](benchmarks.md#retrospective-memory-engine)

## Implementation files

- `cognicore/history/` contains the ChatGPT adapter, provenance schema, engine, extractor, CLI, REST routes, and MCP tool registration.
- `cognicore/memory/sqlite_backend.py`, `cognicore/cli.py`, `cognicore/server/app.py`, and `cognicore/mcp/server.py` provide the shared backend and entry-point integration.
- `tests/test_history.py` exercises imports, deletion, provenance, evidence, authorization, CLI, and a real MCP stdio session.
- `benchmarks/history_benchmark.py` and `examples/chatgpt-history.json` provide the benchmark and tutorial fixture. `.gitignore` permits that specific synthetic JSON file.
- `README.md`, `PRIVACY.md`, `docs/architecture.md`, `docs/api_reference.md`, `docs/mcp-tools.md`, `docs/benchmarks.md`, and `examples/README.md` link to and describe the feature.

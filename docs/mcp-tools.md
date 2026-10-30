# Hosted MCP tool reference

The hosted server at `https://mcp.cognicoree.tech/mcp/stream` exposes 31 tools. This page lists
what each one does, so you can tell before you call it whether it reads your data, changes it, or
talks to a third-party service.

Every tool carries a title and behaviour annotations, which MCP clients use to decide when to ask
you for confirmation:

| Annotation | Meaning |
|---|---|
| **read** | Reads your data only (`readOnlyHint`) |
| **write** | Changes your data (`destructiveHint: false`) |
| **delete** | Removes data (`destructiveHint`) |
| **external** | Sends a request to a service outside CogniCore (`openWorldHint`) |

All tools operate on the signed-in user's own database. No tool can read or write another account's
data; the tools that take another user's id check it and refuse. See
[Remote MCP server](remote-mcp-server.md) for sign-in and deployment.

---

## Memory

The core of CogniCore. Search is BM25 over your own entries — no embedding service involved.

| Tool | Kind | What it does |
|---|---|---|
| `cognicore_remember` | write | Save a fact. Optional `category`, and `scope` of `user` (applies everywhere) or `project`. |
| `cognicore_recall` | read | Search your memories and return the `top_k` most relevant (default 5). |
| `cognicore_list` | read | List recent memories, filtered by category or scope. |
| `cognicore_forget` | delete | Delete one memory by its id. |
| `cognicore_stats` | read | Counts, categories and storage size for your memory store. |
| `cognicore_compress_context` | write | Summarize a long conversation, keeping the last `keep_last_n` turns verbatim. |
| `cognicore_save_session` | write | Store a summary of the current session under an optional name. |
| `cognicore_resume_session` | read | Return the last `last_n_sessions` summaries, with action items. |

## Experiences

A record of what was *tried*, not just what was said. An experience starts as a candidate and
becomes verified only when you supply evidence that the solution worked.

| Tool | Kind | What it does |
|---|---|---|
| `cognicore_record_experience` | write | Record a solved problem: the task, attempts that failed, what worked and why, plus the environment (Python version, framework, dependencies) it worked in. |
| `cognicore_verify_experience` | write | Promote a candidate experience to verified by attaching evidence, such as test results or command output. |
| `cognicore_recall_experience` | read | Find relevant past experiences before starting a task. Includes known failures, and can require verified ones only. |
| `cognicore_check_experience` | read | Check whether an experience still applies after your environment changed. |
| `cognicore_share_experience` | write | Transfer verified experiences to another agent id, failure warnings included. Only verified experiences with intact provenance are sent. |

## Figma

The one integration that calls a third-party API. `cognicore_figma_sync` requests your file from
`api.figma.com` and stores the design data in your memory; every other tool here then works from
that stored copy, with no token needed.

Pass your Figma personal access token as the `access_token` argument. It is used for that request
only and is never stored. On the hosted server you must pass it explicitly — the
`FIGMA_ACCESS_TOKEN` environment variable is a deployment setting, not your token. Prefer a
read-only token, and revoke it when you are done.

| Tool | Kind | What it does |
|---|---|---|
| `cognicore_figma_sync` | external | Pull styles, components, variables and file metadata from a Figma file into memory. Takes the `file_key` from the file URL. |
| `cognicore_figma_recall` | read | Return the stored design tokens, typography, colors and variables as JSON. |
| `cognicore_figma_design_system` | read | Return the stored design system as a whole. |
| `cognicore_figma_check_component` | read | Check an implementation against a stored component's definition. |
| `cognicore_figma_record_implementation` | write | Record that a component was implemented, and where. |
| `cognicore_figma_recommend` | read | Translate stored tokens into instructions for a target tool (`cursor`, `claude`, `elevenlabs`). |

## ElevenLabs preferences

**These tools store and recommend voice settings. They do not call the ElevenLabs API and do not
generate audio.** Use them alongside ElevenLabs' own MCP server or your own API calls: ask
`cognicore_elevenlabs_recall` for settings, generate the audio yourself, then report the outcome
back with `cognicore_elevenlabs_learn` and `cognicore_elevenlabs_feedback`. Over time the stored
ratings make `cognicore_elevenlabs_recommend` more useful. A `voice_id` you pass is stored as given
and is not checked against your ElevenLabs account.

| Tool | Kind | What it does |
|---|---|---|
| `cognicore_elevenlabs_sync` | write | Store voice preferences: `voice_id`, stability, similarity boost, style, speed, model, content type, audience, tone, and custom pronunciations. |
| `cognicore_elevenlabs_recall` | read | Return the stored preferences as ready-to-use API parameters, optionally with usage patterns. |
| `cognicore_elevenlabs_learn` | write | Record that a generation happened, returning a `generation_id` to attach feedback to. |
| `cognicore_elevenlabs_feedback` | write | Attach a rating (1–5), engagement percentage and audience comments to a `generation_id`. |
| `cognicore_elevenlabs_recommend` | read | Recommend a voice and settings from past outcomes and feedback. |

## Cognitive Fabric

Derives semantic rules from what you have stored and restates them for the tool you are using.

| Tool | Kind | What it does |
|---|---|---|
| `cognicore_fabric_recommend` | write | Return the active rules and recommendations, translated for a named tool and task. |
| `cognicore_fabric_sync_figma` | external | Sync a Figma file into the Fabric so it can derive design rules from it. |

## Memory marketplace (experimental)

An experiment in agent-to-agent memory exchange. The tools work and are isolated per account, but
there is no live market behind them: discovery and purchase run against memories that users of this
server have explicitly listed, and **no real money is involved**. Nothing of yours is listed unless
you list it. Treat this group as a demonstration.

| Tool | Kind | What it does |
|---|---|---|
| `cognicore_list_for_sale` | write | List your own memories as available to other signed-in users. |
| `cognicore_discover_sellers` | read | Find accounts offering memories, filtered by reputation and notional price. |
| `cognicore_purchase_memory` | write | Copy listed memories from another account into yours. The seller must have listed them. |
| `cognicore_value_my_memory` | write | Estimate a notional value for your memories. |
| `cognicore_reputation` | write | Return an account's marketplace reputation. |

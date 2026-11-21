# CogniCore Memory — Claude Plugin

> Give Claude a memory that survives across conversations.

Claude forgets everything when a conversation ends. CogniCore Memory connects Claude to the hosted
CogniCore server, so you can save decisions, preferences, bug fixes and session handoffs, and
recall them in a later conversation.

| Without CogniCore | With CogniCore |
|---|---|
| Re-explain your stack every chat | Claude can recall your stack |
| Repeat your preferences each session | Claude can recall what you like |
| Forget which bug was already fixed | Claude can recall the fix |

---

## Install

In Claude Code:

```
/plugin marketplace add cognicore-dev/cognicore-env
/plugin install cognicore-memory@cognicore-beta-marketplace
```

The first time a CogniCore tool runs, your browser opens to sign in with **GitHub or Google**.
The two are separate accounts: memories saved while signed in with one are not visible from the other.

---

## What this plugin contains

| Part | What it does |
|---|---|
| MCP server (`.mcp.json`) | Connects to `https://mcp.cognicoree.tech/mcp/stream` over Streamable HTTP. No local code runs. |
| Commands | `/remember`, `/recall`, `/memory-list`, `/forget` |
| Skill `memory` | Guidance on when to recall context and when to offer to save it |
| Agent `memory-assistant` | Recalls relevant memories at the start of a conversation and **asks you before saving** anything, unless you said "remember this" |

The plugin has no hooks. It does not read your files or transcripts. Only what Claude passes to a
CogniCore tool call is sent to the server.

---

## Tools

The hosted server exposes **31 tools**. Installing the plugin makes all of them available to
Claude; each one runs only when Claude calls it, and every tool is labelled as read-only, write,
delete or external write so your client can ask before it runs.

**Memory (5):** `cognicore_remember`, `cognicore_recall`, `cognicore_list`, `cognicore_forget`,
`cognicore_stats` — save, search, list, delete and count your memories.

**Sessions (3):** `cognicore_save_session`, `cognicore_resume_session`, `cognicore_compress_context` —
save a handoff of the current work (goal, state, decisions, open tasks, exact values) and resume it
later, in this or another app. Conversations longer than about 150,000 tokens passed to these tools
have their older part summarised and saved to your memory.

**Experience (5):** `cognicore_record_experience`, `cognicore_verify_experience`,
`cognicore_recall_experience`, `cognicore_check_experience`, `cognicore_share_experience` — record
what worked and what failed, with evidence, and check whether a past fix still applies.

**Figma (8):** `cognicore_figma_sync`, `cognicore_figma_recall`, `cognicore_figma_check_component`,
`cognicore_figma_record_implementation`, `cognicore_figma_design_system`, `cognicore_figma_recommend`,
`cognicore_fabric_recommend`, `cognicore_fabric_sync_figma` — store design tokens, styles and
components from a Figma file you choose. The two sync tools call Figma's API with a Figma access
token you provide; the token is used for that request only and is not stored.

**ElevenLabs (5):** `cognicore_elevenlabs_sync`, `cognicore_elevenlabs_recall`,
`cognicore_elevenlabs_learn`, `cognicore_elevenlabs_feedback`, `cognicore_elevenlabs_recommend` —
store voice preferences and settings you give. CogniCore does not contact ElevenLabs.

**Marketplace (5, opt-in):** `cognicore_list_for_sale`, `cognicore_purchase_memory`,
`cognicore_value_my_memory`, `cognicore_reputation`, `cognicore_discover_sellers`.
Nothing is shared unless you use these tools. Using any of them creates a marketplace profile that
other signed-in users can see (your account ID, categories, reputation and transfer records). Only
memories you list with `cognicore_list_for_sale` can be found and copied by other users. Prices
are estimates; no money changes hands.

---

## Slash Commands

| Command | Example |
|---|---|
| `/remember <text>` | `/remember I use FastAPI and PostgreSQL` |
| `/recall <query>` | `/recall what database do I use` |
| `/memory-list` | Browse everything stored |
| `/forget <id>` | `/forget 3` |

```
/remember This project uses FastAPI and PostgreSQL --scope project
/remember I prefer TypeScript over JavaScript --scope user
/recall what database does this project use
/memory-list
```

## How Search Works

Recall uses **BM25** ranking, so specific terms rank higher than common words, and natural
language queries such as "auth bug fix" or "my preferred stack" work well.

## Scopes

- **`user`** — personal preferences that apply everywhere (the default)
- **`project`** — decisions specific to the current codebase

---

## Where Your Memories Live

Memories are stored on the hosted CogniCore server at `https://mcp.cognicoree.tech`, in a database
of your own, separate from every other user's. The server runs on Microsoft Azure.
Databases are backed up nightly and each backup is deleted after 30 days (the storage provider may
keep it up to 7 more days); server logs are kept for up to 14 days. Sarvam AI is used only to
shorten sessions synced from your computer with the separate `cognicore login` command, not
sessions saved through this plugin.

- Other users cannot read your memories unless you list them with `cognicore_list_for_sale`.
- Delete any memory at any time with `/forget <id>`.
- To delete or export everything, email the address in the
  [privacy policy](https://github.com/cognicore-dev/cognicore-env/blob/main/PRIVACY.md), which also
  covers what is collected, which providers process it and how long it is kept.

## Self-Hosting

The hosted server is the same code as this repository.
[docs/remote-mcp-server.md](https://github.com/cognicore-dev/cognicore-env/blob/main/docs/remote-mcp-server.md)
covers deploying it with Docker Compose. Once it runs, point `url` in this plugin's `.mcp.json` at
your own server.

For memory with no server at all, `pip install cognicore-env` and run the local stdio MCP server —
see the [README](https://github.com/cognicore-dev/cognicore-env#mcp-server).

## License

Apache License 2.0 © CogniCore

# CogniCore Memory — Claude Plugin

> Give Claude a memory that survives across conversations.

---

## The Problem

Claude forgets everything when a conversation ends. Every time you start a new chat, you have to re-explain your project, your preferences, your stack.

**CogniCore fixes that.**

---

## What It Does

CogniCore lets Claude store and recall information across sessions. You can save decisions, preferences, bug fixes, or any fact — and Claude will remember them the next time you ask.

| Without CogniCore | With CogniCore |
|---|---|
| Re-explain your stack every chat | Claude already knows your stack |
| Repeat your preferences each session | Claude remembers what you like |
| Forget which bug was already fixed | Claude can recall the fix |

---

## Tools

These run automatically when you ask Claude to remember or recall something:

| Tool | What it does |
|---|---|
| `cognicore_remember` | Save a memory |
| `cognicore_recall` | Search your memories |
| `cognicore_list` | See all stored memories |
| `cognicore_forget` | Delete a memory by its ID |

---

## Automatic Memory (Claude Code)

The plugin's hooks make memory automatic, so Claude doesn't have to remember to save:

| When | What happens |
|---|---|
| A session starts in a project | That project's last sessions are added as context (goal, state, open tasks, decisions, facts), within a 1,000-token budget. Nothing is added in a project with no history. |
| After each reply, before compaction, at session end | The session is saved from Claude Code's transcript: what you and Claude said, and which files were edited. |

- **No model calls and no background tokens.** Capture is rule based, and memory is ready when the hook returns, typically in about a second.
- **What is stored:** your prompts and Claude's replies, with detected secrets redacted, plus edited file paths. Tool output, file contents, Claude's thinking and Claude Code's injected system context are never read.
- **Requirements:** the `cognicore-env` package in the Python that runs the hooks (`pip install cognicore-env`). Set `COGNICORE_PYTHON` if that isn't `python` on your PATH.
- **Where:** `~/.cognicore/mcp/mcp_memories.db`, shared with the local MCP server (`cognicore mcp serve`). Change it with `COGNICORE_DATA_DIR`.
- **See what's stored:** `python -m cognicore.memory.viewer` opens a read-only page at http://127.0.0.1:37780 listing sessions by project, with each one's brief and stored handoff.
- **Options:** `COGNICORE_AUTO_CAPTURE=0` stops saving, `COGNICORE_AUTO_RESUME=0` stops the start-of-session context, and `COGNICORE_RESUME_TOKENS` sets the budget.

The hooks write to local memory. The hosted connector in `.mcp.json` keeps its own separate memory.

---

## Slash Commands

You can also trigger memory directly with slash commands:

| Command | Example |
|---|---|
| `/remember <text>` | `/remember I use FastAPI and PostgreSQL` |
| `/recall <query>` | `/recall what database do I use` |
| `/memory-list` | Browse everything stored |
| `/forget <id>` | `/forget 3` |

---

## Quick Example

```
/remember This project uses FastAPI and PostgreSQL --scope project
/remember I prefer TypeScript over JavaScript --scope user
/recall what database does this project use
/memory-list
```

---

## How Search Works

When you recall something, CogniCore uses **BM25** — the same algorithm used by Elasticsearch — to find the most relevant memories. Specific terms rank higher than common words. Natural language works well:

- `"who built this"` → finds the memory mentioning the person
- `"auth bug fix"` → finds debugging memories about authentication
- `"my preferred stack"` → finds your preference memories

---

## Scopes

When saving a memory, you can optionally tag it with a scope:

- **`user`** — personal preferences that apply everywhere (this is the default)
- **`project`** — decisions specific to the current codebase

---

## Privacy

- Your memories are stored in **your own Railway deployment** — not on CogniCore's servers.
- Each user's memories are isolated by a unique hash of their Claude identity.
- You can delete any memory at any time with `/forget <id>`.

---

## Self-Hosting

Deploy your own CogniCore memory backend to Railway in under 5 minutes:

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template)

After deploying:
1. Set `JWT_SECRET` in your Railway environment variables
2. Update the server URL in `.mcp.json` to point to your deployment

See the [deployment guide](https://github.com/cognicore-dev/cognicore-my-openenv#deployment) for full instructions.

---

## License

MIT © CogniCore

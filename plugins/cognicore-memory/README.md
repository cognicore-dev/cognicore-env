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

## Where Your Memories Live

This plugin connects to the hosted CogniCore server at `https://mcp.cognicoree.tech`. You sign in
with GitHub, and your memories are stored there, in a database of your own, separate from every
other user's.

- Other users cannot read your memories unless you list them with the marketplace tool `cognicore_list_for_sale`.
- You can delete any memory at any time with `/forget <id>`.
- To delete or export everything, email the address in the [privacy policy](https://github.com/cognicore-dev/cognicore-env/blob/main/PRIVACY.md),
  which also covers what is collected and how long it is kept.

---

## Self-Hosting

If you would rather run the server yourself, the hosted server is the same code as this repository,
and [docs/remote-mcp-server.md](https://github.com/cognicore-dev/cognicore-env/blob/main/docs/remote-mcp-server.md)
covers deploying it with Docker Compose behind a reverse proxy, including the GitHub OAuth app and
the environment variables it needs.

Once it is running, point `url` in this plugin's `.mcp.json` at your own server.

For memory with no server at all, `pip install cognicore-env` and run the local stdio MCP server —
see the [README](https://github.com/cognicore-dev/cognicore-env#mcp-server).

---

## License

MIT © CogniCore

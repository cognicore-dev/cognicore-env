# CogniCore Claude Desktop Extension

This extension integrates CogniCore's persistent memory capabilities directly into Claude Desktop using the Model Context Protocol (MCP).

> **Local install only.** Anthropic has deprecated desktop-extension listings, and
> its directory no longer accepts local servers packaged as MCP Bundles (`.mcpb`).
> This extension still installs and runs locally as described below. To share
> CogniCore through the directory instead, use the hosted connector
> ([remote-mcp-server.md](remote-mcp-server.md)) or the Claude Code plugin
> ([plugins/cognicore-memory](../plugins/cognicore-memory/README.md)).

## Overview

Claude Desktop sessions are typically ephemeral. When you start a new chat, Claude forgets what you discussed previously.

With the **CogniCore Extension**, Claude gains a permanent, long-term memory. It can remember your preferences, project technology stacks, key facts, and prior decisions across all conversations.

## Quick Install (One-Click)

If you have the bundled `.mcpb` package:

1. Drag and drop `cognicore-memory.mcpb` into your Claude Desktop application.
2. Accept the installation prompt.
3. The server will start automatically using the `uv` package manager (which is bundled with Claude Desktop).

## Manual Install (Via Settings)

1. Open your Claude Desktop settings (`claude_desktop_config.json`):
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`

2. Add the following entry:
```json
{
  "mcpServers": {
    "cognicore-memory": {
      "command": "uvx",
      "args": [
        "cognicore-env[extension]",
        "cognicore-extension"
      ]
    }
  }
}
```

3. Restart Claude Desktop.

## Usage Guide

You can now ask Claude to remember important details for the future. 

### Storing Memories
Just tell Claude to remember something:
- "Please remember that all my backend code uses FastAPI."
- "Keep in mind that I prefer dark mode interfaces."
- "For this project, the database URL is always `postgresql://localhost:5432/dev`."

Claude will use the `cognicore_remember` tool to save these facts.

### Recalling Memories
You don't always need to explicitly ask Claude to search its memory. If you ask a question where a previous preference might be relevant, Claude will proactively use the `cognicore_recall` tool.

However, you can explicitly ask:
- "What did I tell you my preferred frontend framework was?"
- "Do you remember the database URL for this project?"

### Managing Memories
If a fact changes, you can ask Claude to update its memory:
- "Forget the old database URL, the new one is `postgresql://db.prod:5432/main`."
- "List everything you remember about my 'preferences' category."

Claude will use the `cognicore_forget` and `cognicore_list` tools.

## Architecture

The extension uses a dedicated `SQLiteMemoryBackend` designed specifically for long-term human-to-AI factual storage. 
- **Privacy:** 100% Local. No data is sent to the cloud.
- **Storage:** Memories are stored in `~/.cognicore/extension/memory.db`.
- **Search:** Uses fast, deterministic SQLite FTS5 (lexical) search by default.

## Enabling Semantic Search

For advanced semantic similarity search (where "frontend framework" matches "React"), you can enable local embeddings via `sentence-transformers`:

1. Update your config args to include the `embeddings` extra:
   ```json
   "args": ["cognicore-env[extension,embeddings]", "cognicore-extension"]
   ```
2. Enable it via environment variables in the config:
   ```json
   "env": {
     "COGNICORE_USE_SEMANTIC": "1"
   }
   ```

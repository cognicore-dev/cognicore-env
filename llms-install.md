# Installing CogniCore for Cline

CogniCore is an MCP server that gives Cline memory between sessions. It runs locally, needs no API key,
and stores everything in SQLite on the user's machine.

## 1. Install

Requires Python 3.10 or newer. Install the `cognicore-env` package with the `mcp` extra:

```bash
pipx install "cognicore-env[mcp]"
```

or, with uv:

```bash
uv tool install "cognicore-env[mcp]"
```

This puts the `cognicore-mcp` command on the PATH. If the shell can't find it afterwards, run
`pipx ensurepath` (or `uv tool update-shell`) and open a new terminal.

Check that the command is found:

```bash
which cognicore-mcp      # Windows: where cognicore-mcp
```

## 2. Add the server to Cline

Add this entry to Cline's MCP settings file (`cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "cognicore": {
      "command": "cognicore-mcp",
      "args": [],
      "env": {
        "COGNICORE_TOOLSETS": "memory,session"
      },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

- `COGNICORE_TOOLSETS=memory,session` loads the six memory and session tools Cline needs. Remove it to load
  all ten tools.
- No API key or account is needed.
- If Cline can't start `cognicore-mcp`, use the full path to the command instead: run `pipx list` (or
  `uv tool dir`) to find it. On Windows it ends in `cognicore-mcp.exe`.

## 3. Verify

Ask Cline: **"Call cognicore_stats."** It should report the data directory and memory counts (all zero on
a new install).

## How Cline uses it

The server sends instructions with its tools, so no extra prompt is needed:

- At the start of a task, Cline calls `cognicore_resume_session` and gets a short brief of earlier work:
  goal, current state, open tasks, decisions with their reasons, and exact values.
- Before finishing, Cline calls `cognicore_save_session` with a handoff of the work.
- `cognicore_remember` and `cognicore_recall` store and search individual facts.

## Data and privacy

- Memory is stored locally in `~/.cognicore/mcp` (set `COGNICORE_DATA_DIR` to use another folder).
- Nothing leaves the machine unless the user runs `cognicore login` to turn on optional sync.
- Values that look like API keys or tokens are redacted before a session is stored.

## Uninstall

```bash
pipx uninstall cognicore-env    # or: uv tool uninstall cognicore-env
```

Then remove the `cognicore` entry from `cline_mcp_settings.json`. Delete `~/.cognicore/mcp` to erase the
memory.

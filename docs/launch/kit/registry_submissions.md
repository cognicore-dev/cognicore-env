# Registry and directory submissions

## Official MCP registry (`server.json`)

`server.json` currently lists only the hosted server. At the next release, add the local package so registry clients can run it locally. This is a proposed change, not yet applied; bump the version with the release:

```json
"version": "0.12.0",
"packages": [
  {
    "registryType": "pypi",
    "identifier": "cognicore-env",
    "version": "0.12.0",
    "runtimeHint": "uvx",
    "transport": { "type": "stdio" },
    "environmentVariables": [
      { "name": "COGNICORE_TOOLSETS", "description": "Tool groups to load, e.g. memory,session", "isRequired": false }
    ]
  }
]
```

Update `description` too: "Memory that moves between agents (Claude Code, Codex, any MCP client). Local, rule-based capture, ~450-token briefs." Publish with the registry's `mcp-publisher` after the PyPI release.

## Smithery and Glama

- **Name:** CogniCore
- **Short:** Memory that moves between agents: local, automatic, about 450-token briefs.
- **Install (stdio):** `uvx --from "cognicore-env[mcp]" cognicore-mcp`, with env `COGNICORE_TOOLSETS=memory,session`
- **Tags:** memory, context, claude-code, codex, sessions, handoff

## Claude Code plugin directory

Submit `plugins/cognicore` (it passes `claude plugin validate`). It needs the icon first; see the kit README.

## Awesome lists

Open one pull request each, against awesome-claude-code, awesome-mcp-servers and awesome-codex. Entry line:

> **[CogniCore](https://github.com/cognicore-dev/cognicore-env/tree/main/plugins/cognicore)**: Memory that moves between Claude Code and Codex. Saves sessions from transcripts with no model calls, resumes with about 450-token briefs. Local.

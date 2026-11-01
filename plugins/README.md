# Plugins

Client-side packages that connect Claude to CogniCore. They hold configuration,
commands and skills. The server code they talk to lives in the `cognicore`
package.

| Plugin | Client | What it does | Talks to |
|---|---|---|---|
| [`cognicore-memory/`](cognicore-memory/) | Claude Code | Persistent memory: `/remember`, `/recall`, `/memory-list`, `/forget`, plus a memory skill and agent | The hosted connector at `https://mcp.cognicoree.tech/mcp/stream` ([`cognicore/connectors/remote.py`](../cognicore/connectors/remote.py)) |
| [`cognicore-experience/`](cognicore-experience/) | Claude Code | Structured, evidence-gated experience memory: failed approaches, verified fixes, environment checks | A local MCP server started from [`scripts/server.py`](cognicore-experience/scripts/server.py) |
| [`claude-desktop-extension/`](claude-desktop-extension/) | Claude Desktop | Local-first memory in SQLite, packaged as a Desktop extension | The local stdio server in [`cognicore/extension/server.py`](../cognicore/extension/server.py), built by [`scripts/build_extension.py`](../scripts/build_extension.py) |

The Claude Code marketplace definition is [`.claude-plugin/marketplace.json`](../.claude-plugin/marketplace.json)
at the repository root, where Claude Code looks for it. It lists `cognicore-memory`
only; `cognicore-experience` and the Desktop extension are installed from this
repository rather than from the marketplace.

## Tests

`cognicore-experience/tests/` has its own suite:

```bash
pytest plugins/cognicore-experience/tests
```

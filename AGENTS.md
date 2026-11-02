# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.
For the full contributor guide see [CONTRIBUTING.md](CONTRIBUTING.md).

## Project

CogniCore NEXUS gives AI agents persistent memory, reflection and adaptive
execution. It ships as a Python package (`cognicore`), a local MCP server, and a
hosted multi-user MCP server at `https://mcp.cognicoree.tech` used by the Claude
and ChatGPT connectors.

## Setup and checks

```bash
pip install -e ".[dev]"            # Python 3.10+
pytest tests/ -n auto -q           # full suite, what CI runs
pytest tests/test_remote_*.py -q   # hosted MCP server only
ruff check <files you changed>     # line length 120
```

The codebase is not ruff-clean yet. Fix lint in the lines you touch, but do not
reformat or mass-fix unrelated files in the same change.

## Layout

| Path | What lives there |
|---|---|
| `cognicore/connectors/remote.py` | Hosted MCP server (FastMCP, Streamable HTTP at `/mcp/stream`, SSE at `/mcp/sse`) |
| `cognicore/connectors/oauth.py` | OAuth 2.1 authorization server that delegates sign-in to GitHub |
| `cognicore/mcp/server.py` | Local stdio MCP server |
| `cognicore/memory/` | Memory backends (SQLite is the default) |
| `cognicore/_compat.py` | Aliases for old import paths such as `cognicore.extension.remote`; keep them working |
| `tests/` | pytest suite |
| `scripts/checks/verify_remote_mcp.py` | Smoke test against a deployed server |
| `docs/remote-mcp-server.md` | Deployment and environment variables |

## Rules for the hosted MCP server

- **Register every tool with `_tool(title, hints)`**, never a bare `@mcp.tool()`.
  Use `_READ`, `_WRITE`, `_DELETE` or `_EXTERNAL_WRITE`. Clients use these hints
  to decide when to ask the user, and `tests/test_remote_tool_annotations.py`
  fails on any tool without them.
- **Write tool descriptions that describe, never instruct.** A description says what the
  tool does; it must not tell the model when to call it ("call this before …") or name
  another tool. Clients feed these strings to the model, so an instruction there is a
  prompt-injection surface, and the connector directory rejects it.
  `tests/test_remote_tool_descriptions.py` fails on both.
- **Get storage through `get_backend(ctx)`.** It resolves the signed-in user and
  opens their own database. Do not open database files or build paths yourself.
- **Never read or write another user's data.** Users are identified as
  `github:<id>`, and GitHub ids are public, so any tool argument that names
  another user is a cross-account path. Check it against `_authenticated_user(ctx)`
  and refuse, as `cognicore_share_experience` does. Cover new cases in
  `tests/test_remote_tenant_isolation.py`.
- Changes to OAuth redirect handling need a test in `tests/test_remote_oauth.py`,
  including a lookalike URL that must be rejected.

## Secrets and security

- Never commit API keys, tokens or `.env` files. Read secrets from environment
  variables and fail clearly when they are missing; no hardcoded fallbacks.
- SecurityBot scans every push. If a finding is a false positive, add
  `# nosec <rule> - <reason>` on that line rather than weakening the rule.
- Build SQL with `?` parameters. Interpolate only values that cannot come from
  a user (integers, fixed identifiers), and say so in a comment.

## Pull requests

- Branch from `main`; one topic per PR.
- CI must pass on Python 3.10, 3.11 and 3.12.
- Keep the public API and old import paths backwards compatible.

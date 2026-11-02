# Reddit posts

Post as yourself, in each sub's own tone, and check each sub's self-promotion rules first. Use one variant per sub, not cross-posts.

## r/ClaudeAI

**Title:** I made Claude Code remember my work between sessions, and hand it over to Codex (open source, benchmarked vs claude-mem)

Every new Claude Code session I was re-explaining the ticket, the decisions and what was left. CogniCore is a plugin that saves each session from the transcript (no model calls) and gives the next session a short brief: goal, state, open tasks, decisions with reasons, and exact IDs, versions and paths.

On real Claude Code sessions vs claude-mem, both got every deferred follow-up done. CogniCore used 2.4–4.1× fewer new tokens and had memory ready in about a second instead of 1–2 minutes. The harness is in the repo if you want to check.

`pipx install "cognicore-env[mcp]" && cognicore install`

Repo: https://github.com/cognicore-dev/cognicore-env/tree/main/plugins/cognicore. Feedback welcome, especially on long, messy sessions.

## r/ChatGPTCoding

**Title:** Hand off work between Claude Code and Codex without re-explaining: open-source memory plugin

Lead with the cross-agent demo (`docs/launch/kit/cognicore-demo.gif`), then: same body as above, opening with "Start a task in Claude Code, continue it in Codex".

## r/cursor

**Title:** Local memory that carries context between sessions and agents (works as an MCP server in Cursor)

Make it clear that Cursor gets the MCP tools, while the automatic hooks are Claude Code and Codex only, so there's no overpromising.

## r/LocalLLaMA

**Title:** Agent memory with zero model calls to save: rule-based capture, ~450-token briefs, all local

Lead with local-first and privacy: nothing leaves the machine unless you opt into AI compression.

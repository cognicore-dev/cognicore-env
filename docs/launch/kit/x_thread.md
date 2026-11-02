# X / LinkedIn thread

1/ Start a task in Claude Code. Close it. Open Codex.

It already knows the ticket, the decisions, and the follow-up you left for later.

CogniCore: open-source memory that moves between agents. 🧵 [demo GIF]

2/ It saves each session from the agent's own transcript, with zero model calls.

At the next start it injects a brief of about 450 tokens: goal, state, open tasks, decisions (with the reasons), and exact IDs, versions and paths.

3/ I benchmarked it against claude-mem on real Claude Code and Codex sessions.

Both: every deferred follow-up done.
CogniCore: 2.4–4.1× fewer new tokens, memory ready in about 1 s instead of 70–130 s.

The harness is in the repo, so you can rerun it.

4/ Local by default. It never reads tool output, file contents or the model's thinking. Secrets are redacted.

For very long sessions, there's optional AI compression with Claude.

5/ Install:
pipx install "cognicore-env[mcp]" && cognicore install

github.com/cognicore-dev/cognicore-env

What should it remember that it doesn't?

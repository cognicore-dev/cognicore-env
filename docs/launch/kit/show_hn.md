# Show HN

**Title** (80 characters max):

> Show HN: CogniCore – memory that moves between Claude Code and Codex

**URL:** https://github.com/cognicore-dev/cognicore-env/tree/main/plugins/cognicore

**Text:**

I kept re-explaining my work every time I switched from Claude Code to Codex, or just started a new session. CogniCore is an open-source plugin that fixes that, and it costs very few tokens.

How it works: hooks save each session from the agent's own transcript (no model calls), and at the next start they inject a short brief for that project: goal, where things stand, open tasks, decisions with their reasons, and exact values like ticket IDs, versions and paths. It works across agents, so work saved in Claude Code resumes in Codex, and the other way round.

I benchmarked it against claude-mem on real sessions. Each task: session 1 does part of a job and hears some facts, its transcript is moved out of reach, and a cold session 2 must finish a deferred follow-up and answer 8 questions.

- Both got every follow-up done, in Claude Code → Claude Code and Claude Code → Codex.
- CogniCore used 2.4–4.1× fewer new tokens (counting claude-mem's background summarizing), and its brief was about 450 tokens against about 1,080.
- Memory is ready about a second after a session ends, against 70–130 s.

The harness is in the repo (`scripts/bench_realworld.py`) if you want to rerun it or poke holes in it. It's only three tasks so far, and I'd welcome harder ones.

Everything is local (`~/.cognicore/mcp`). It never reads tool output, file contents, or the model's thinking, and it redacts secrets. Install:

    pipx install "cognicore-env[mcp]" && cognicore install

I'd like feedback on what it misses in your long sessions. Optional AI compression (Claude) exists for those, but it's off by default.

---

**First comment** (post right after, to answer the obvious questions):

- **Why not just use claude-mem?** It's good, and it summarizes every tool call with a model. CogniCore saves by rule, so capture is free and instant; AI summarizing is opt-in for long sessions. It's also built around handing off between different agents.
- **Which agents?** Claude Code and Codex have hooks, so it's automatic there. Any other MCP client can use the tools.
- **What's stored?** Your prompts, the agent's replies, and edited file paths, with secrets redacted. Browse it with `python -m cognicore.memory.viewer`.

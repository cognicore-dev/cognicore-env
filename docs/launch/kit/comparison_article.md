# claude-mem vs CogniCore: measured on real agent sessions

*Draft for DEV, Medium or a blog. Credit claude-mem fairly; the article is credible because it shows where claude-mem wins.*

Coding agents forget everything when a session ends. Two open-source tools fix that for Claude Code: claude-mem, the popular one, and CogniCore. Instead of comparing feature lists, I ran both on the same real tasks and measured.

## The test

Three small coding tasks: a rounding bug, retry logic, and a slow export job. Each runs as two separate agent sessions:

1. **Session 1** does part of the work and is told facts only in conversation: a ticket ID, why a decision was made, who reviews it, a follow-up deferred to "next time".
2. **Between sessions,** session 1's transcript is moved out of reach. Without that, a memoryless agent can sometimes dig the old log out of `~/.claude/projects`.
3. **Session 2** starts cold with "continue where the previous session left off", must finish the follow-up, and answers 8 questions. A script scores the answers and checks the follow-up was really done.

Token counts come from the agents' own usage reports. For claude-mem, its background summarizing is added from its own token counter. The harness is open: `scripts/bench_realworld.py`.

## Results

| | Claude → Claude: CogniCore | claude-mem | Claude → Codex: CogniCore | claude-mem |
|---|---|---|---|---|
| Follow-ups done | 3/3 | 3/3 | 3/3 | 3/3 |
| Questions right | 24/24 | 24/24 | 23/24 | 24/24 |
| New tokens incl. background | **52.6k** | 215.9k | **60.3k** | 146.5k |
| Memory ready after a session | **~1 s** | 70–80 s | **~1 s** | 80–130 s |
| Brief at next start | ~450 tok | – | ~450 tok | ~1,080 tok |

*Without memory, session 2 got 8/24 questions and finished 0 of 3 follow-ups.*

## Why the token gap

claude-mem asks a model to summarize every tool call in the background, which costs tokens and takes time. CogniCore extracts by rule from the transcript: goal, state, decisions with reasons, open tasks, and every exact value verbatim. That's free and instant, and it produces a smaller brief.

## Where claude-mem is stronger

- **Long, messy sessions.** Model summaries can condense prose that rules keep word for word. CogniCore has opt-in AI compression for this; it isn't benchmarked here yet.
- **Maturity and ecosystem.** claude-mem has a much bigger community, a polished viewer, and support for more agents.

## Try it

    pipx install "cognicore-env[mcp]" && cognicore install

Then rerun the benchmark on your own tasks and tell me what you find.

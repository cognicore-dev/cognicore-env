# CogniCore

**Memory that moves between your agents.** Work in Claude Code, close it, open Codex, and it carries on: same ticket, same decisions, same open follow-up. Nothing to re-explain.

![Claude Code hands its work to Codex through CogniCore memory](../../docs/launch/kit/cognicore-demo.gif)

```bash
pipx install "cognicore-env[mcp]"   # or: uv tool install "cognicore-env[mcp]"
cognicore install                   # sets up Claude Code and Codex
```

Then restart Claude Code. In Codex, run `/hooks` once and trust the three CogniCore hooks.

Claude Code only? After the `pipx` line, install the plugin from inside Claude Code:

```
/plugin install cognicore --marketplace cognicore-dev/cognicore-env
```

## Why CogniCore

Measured on real Claude Code and Codex sessions against claude-mem 15.7.0. Both systems got every follow-up right ([method and full results](../../docs/transfer-audit.md#head-to-head-with-claude-mem-on-current-versions-2026-11-01)):

| | CogniCore | claude-mem |
|---|---|---|
| Hands work from Claude Code to Codex | ✅ | ✅ |
| New tokens per 3-task benchmark (incl. background) | **53k–60k** | 147k–216k |
| Memory ready after a session ends | **~1 second** | 70–130 seconds |
| Brief injected at the next start | **~450 tokens** | ~1,080 tokens |
| Model calls to save a session | **0** (rule-based; optional AI compression) | one per tool use |

- **Automatic.** Hooks save each session from the agent's own transcript and resume the project's latest work at the next start. There's nothing to remember to call.
- **Exact.** IDs, versions, paths, hashes, error codes and numbers are kept verbatim, along with the reasons behind decisions.
- **Small.** The brief is budgeted: most important first (goal, state, open tasks, decisions, facts), with the rest fetchable on demand.
- **Private.** Memory is local (`~/.cognicore/mcp`). CogniCore keeps what you and the agent said, plus the paths of edited files. It never reads tool output, file contents, the model's thinking, or the system context agents inject. Detected secrets are redacted.

## Sync with claude.ai, Claude Desktop and mobile (optional)

```bash
cognicore login      # sign in once with GitHub, in the browser
```

After you sign in, a session saved in Claude Code or Codex is also uploaded to your hosted CogniCore account. Sessions saved through the CogniCore connector in claude.ai, Claude Desktop or the mobile app are downloaded when your next session starts. What's uploaded is the redacted handoff (goal, state, tasks, decisions, facts, values), never the transcript. A new session from another app appears in the brief as one line, and the agent loads it when it's relevant.

- The upload runs in the background, so a turn never waits on the network. The download at session start gives up after 3 seconds, so working offline is unaffected.
- `cognicore sync` syncs now. `cognicore sync status` shows who is signed in. `COGNICORE_SYNC=0` pauses sync. `cognicore logout` turns it off.
- Nothing leaves your machine until you run `cognicore login`.

## What it adds to each turn

The hooks save and resume on their own, so the plugin's MCP server loads one tool, `cognicore_recall`, for looking up details the brief leaves out. That's about 230 tokens per turn, including its instructions (6 tools used about 1,240). Set `COGNICORE_TOOLSETS=memory,session` to get the save, resume and remember tools as well. The hooks inject nothing in a project with no history.

Long sessions keep everything searchable: the brief and what syncs stay short, while statements the brief leaves out are stored on your machine and found with `cognicore_recall`.

## Options

| Variable | Effect |
|---|---|
| `COGNICORE_DATA_DIR` | Where memory lives (default `~/.cognicore/mcp`) |
| `COGNICORE_RESUME_TOKENS` | Budget for the start-of-session brief (default 1000) |
| `COGNICORE_AUTO_CAPTURE=0` / `COGNICORE_AUTO_RESUME=0` | Turn saving / resuming off |
| `COGNICORE_AI_COMPRESS=auto` | Summarize long sessions with Claude. Needs `pip install anthropic` and Anthropic credentials. `COGNICORE_AI_MODEL` picks the model. |
| `COGNICORE_SYNC=0` | Pause sync with your hosted account (after `cognicore login`) |
| `COGNICORE_TOOLSETS` | Which MCP tool groups load (`search` here; `memory,session` adds save, resume and remember; `all` for every tool) |

## See what's stored

```bash
python -m cognicore.memory.viewer     # read-only page at http://127.0.0.1:37780
```

## Remove

```bash
cognicore setup codex --remove        # Codex hooks and AGENTS.md rule
```

In Claude Code, run `/plugin uninstall cognicore`. Delete `~/.cognicore/mcp` to delete the memory itself.

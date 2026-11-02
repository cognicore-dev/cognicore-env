# 30-second demo: Claude Code → Codex

Record at 1080p with a large terminal font. Cut pauses, keep typing at real speed, and caption each shot.

| Time | Screen | Caption |
|---|---|---|
| 0–5 s | Claude Code in `billing-app`. Type: *"BILL-311: German totals are off by a cent. Use Decimal with ROUND_HALF_UP; finance requires commercial rounding. Next time, format_eur must return '12,34 €'. Reviewer is Anika."* | "Tell Claude Code what's going on" |
| 5–10 s | Claude fixes `total_with_tax`, tests pass. Close Claude Code. | "Work done, session closed" |
| 10–14 s | Open Codex in the same folder. The status line shows "Loading CogniCore memory". | "Open a different agent" |
| 14–22 s | Type only: *"continue where we left off"*. Codex edits `format_eur`, the test passes, and it mentions BILL-311 and Anika. | "It picks up the follow-up, unprompted" |
| 22–27 s | Run `python -m cognicore.memory.viewer` and show the session list: one session by claude-code, one by codex. | "One memory, shared by both agents" |
| 27–30 s | End card: `pipx install "cognicore-env[mcp]" && cognicore install`, plus the repo URL. | "Open source, local, about 450 tokens" |

**Before recording:** run `cognicore install`, trust the hooks in Codex via `/hooks`, and start from an empty `~/.cognicore/mcp`, so the viewer shows only the demo.

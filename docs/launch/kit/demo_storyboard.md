# 48-second demo: Claude Code → claude.ai → Codex

Rendered as [cognicore-demo.mp4](cognicore-demo.mp4) and [cognicore-demo.gif](cognicore-demo.gif) from the Remotion project in [video/](video/). The screen text comes from a real `scripts/bench_realworld.py` run. To record a live screen capture instead, use 1080p with a large terminal font, cut pauses, and caption each shot.

| Time | Screen | Caption |
|---|---|---|
| 0–5 s | Claude Code in `billing-app`. Type: *"BILL-311: German totals are off by a cent. Use Decimal with ROUND_HALF_UP; finance requires commercial rounding. Next time, format_eur must return '12,34 €'. Reviewer is Anika."* | "Tell Claude Code what's going on" |
| 5–10 s | Claude fixes `total_with_tax`, tests pass. Close Claude Code. | "Work done, session closed" |
| 10–14 s | Open Codex in the same folder. The status line shows "Loading CogniCore memory". | "Open a different agent" |
| 14–22 s | Type only: *"continue where we left off"*. Codex edits `format_eur`, the test passes, and it mentions BILL-311 and Anika. | "It picks up the follow-up, unprompted" |
| 22–27 s | Run `python -m cognicore.memory.viewer` and show the session list: one session by claude-code, one by codex. | "One memory, shared by both agents" |
| 27–30 s | End card: `pipx install "cognicore-env[mcp]" && cognicore install`, plus the repo URL. | "Open source, local, about 450 tokens" |

**Before recording:** run `cognicore install`, trust the hooks in Codex via `/hooks`, and start from an empty `~/.cognicore/mcp`, so the viewer shows only the demo.

## Version 2 (48 s, rendered)

| Time | Screen | Caption |
|---|---|---|
| 0–3 s | "Every new session starts from zero." with Claude Code, Codex and claude.ai | — |
| 3–13 s | Claude Code session on BILL-311 (as above) | "Tell Claude Code what's going on", then "Work done, session closed" |
| 13–21 s | A claude.ai chat: "What did I do in my last Claude Code session?", then the CogniCore resume call and the answer | "Ask claude.ai. It already knows." |
| 21–33 s | Codex loads the brief and finishes the follow-up | "Open a different agent", then "It picks up the follow-up, unprompted" |
| 33–39 s | Four measured numbers: ~1,000-token brief vs 1.36M history; 228 tokens/turn; 2.4–4.1× fewer than claude-mem; 0 model calls | "Measured, not claimed" |
| 39–44 s | Memory viewer: sessions by codex, claude.ai, claude-code | "One memory for all three" |
| 44–48 s | End card: install line, `cognicore login`, repo, Apache 2.0 | — |

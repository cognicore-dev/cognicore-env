# CogniCore demo video

Source for `../cognicore-demo.mp4` and `../cognicore-demo.gif`: a 48-second clip. Claude Code works on a ticket, then claude.ai and Codex pick up the same work from CogniCore memory, followed by the measured token numbers. It's built with [Remotion](https://www.remotion.dev).

All on-screen text is in `src/data.ts`. It is taken from a real `scripts/bench_realworld.py` run. Edit that file to change what is shown.

```bash
npm i
npx remotion studio                       # preview and edit
npx remotion render CogniCoreDemo out/cognicore-demo.mp4 --codec=h264
npx remotion render CogniCoreDemo out/cognicore-demo.gif --codec=gif --scale=0.5 --every-nth-frame=2
```

| Scene | File | Length |
|---|---|---|
| Every session starts from zero | `src/scenes/Hook.tsx` | 3 s |
| Claude Code session | `src/scenes/ClaudeSession.tsx` | 10 s |
| claude.ai picks it up | `src/scenes/WebRecall.tsx` | 8 s |
| Codex loads the brief | `src/scenes/CodexBrief.tsx` | 4 s |
| Codex continues | `src/scenes/CodexContinues.tsx` | 8 s |
| The numbers (measured) | `src/scenes/Numbers.tsx` | 6 s |
| One memory for all three | `src/scenes/SharedMemory.tsx` | 5 s |
| End card | `src/scenes/EndCard.tsx` | 4 s |

The claude.ai scene is reconstructed: in a real test (2026-11-03), claude.ai called CogniCore's resume tool on its own when asked what happened in Claude Code. Its answer on screen uses only the brief from the benchmark run. Each scene's footer says where its content comes from.

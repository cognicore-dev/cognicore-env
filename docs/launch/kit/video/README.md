# CogniCore demo video

Source for `../cognicore-demo.mp4` and `../cognicore-demo.gif`: a 30-second clip where Claude Code works on a ticket, the session closes, and Codex picks up the same work from CogniCore memory. It's built with [Remotion](https://www.remotion.dev).

All on-screen text is in `src/data.ts`. It is taken from a real `scripts/bench_realworld.py` run. Edit that file to change what is shown.

```bash
npm i
npx remotion studio                       # preview and edit
npx remotion render CogniCoreDemo out/cognicore-demo.mp4 --codec=h264
npx remotion render CogniCoreDemo out/cognicore-demo.gif --codec=gif --scale=0.5 --every-nth-frame=2
```

| Scene | File | Length |
|---|---|---|
| Claude Code session | `src/scenes/ClaudeSession.tsx` | 10 s |
| Codex loads the brief | `src/scenes/CodexBrief.tsx` | 4 s |
| Codex continues | `src/scenes/CodexContinues.tsx` | 8 s |
| One memory, every agent | `src/scenes/SharedMemory.tsx` | 5 s |
| End card | `src/scenes/EndCard.tsx` | 3 s |

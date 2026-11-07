# Launch kit: CogniCore cross-agent memory

Drafts for launching the `cognicore` plugin. Numbers are from [docs/transfer-audit.md](../../transfer-audit.md) and can be rerun with `scripts/bench_realworld.py`.

| File | For |
|---|---|
| [show_hn.md](show_hn.md) | Hacker News "Show HN" post plus a first comment |
| [reddit.md](reddit.md) | r/ClaudeAI, r/ChatGPTCoding, r/cursor and r/LocalLLaMA variants |
| [x_thread.md](x_thread.md) | X / LinkedIn thread |
| [product_hunt.md](product_hunt.md) | Product Hunt listing |
| [comparison_article.md](comparison_article.md) | "claude-mem vs CogniCore, measured" (DEV / Medium / blog) |
| [cognicore-demo.gif](cognicore-demo.gif) / [cognicore-demo.mp4](cognicore-demo.mp4) | The 48-second demo: Claude Code → claude.ai → Codex, plus the measured numbers (GIF 960×540 for posts, MP4 1080p) |
| [demo_storyboard.md](demo_storyboard.md) | Shot list the demo follows |
| [video/](video/) | Remotion source for the demo; edit `src/data.ts` and re-render |
| [registry_submissions.md](registry_submissions.md) | MCP registry, Smithery, Glama, awesome-list entries |

## Before posting

1. **Merge and release.** Merge `feat/memory-transfer` and publish a release, e.g. 0.12.0, with the `cognicore-mcp` and `cognicore-hooks` console scripts. The install line `pipx install "cognicore-env[mcp]" && cognicore install` only works once the release is on PyPI.
2. **Check the install on a clean machine.** Run `cognicore install`, restart Claude Code, then trust the hooks in Codex via `/hooks`.
3. **Add a plugin icon.** Use a square PNG, 512–2048 px, at `plugins/cognicore/.claude-plugin/icon.png`. The plugin directory keeps the first icon it sees.
4. **Demo is ready:** `cognicore-demo.gif` and `cognicore-demo.mp4`. To change it, see [video/README.md](video/README.md).
5. **Post every channel within the same 24–48 hours**, so stars land together for GitHub Trending.

## What to track

Stars in the first 48 hours, PyPI installs, and the issues opened. Answer issues the same day during launch week.

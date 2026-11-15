# Changelog

All notable changes to CogniCore NEXUS are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Changed
- **Sarvam summaries are given room to finish, and `/health` says whether they work.** `sarvam-105b` reasons at length before it answers, and on a real 5k-token session it produced nothing within the old 120 s and 8,000-token limits. The background call now waits up to 300 s and allows 16,000 output tokens. At most about 6,000 tokens of a handoff are sent, newest items first, and items too old to send stay in the handoff as captured, so nothing is lost. `/health` now has `ai_summaries`: whether a key is set (never the key itself), the model, and the outcome and duration of the last attempt, such as `summarised` or `skipped: API error 403`. Unexpected errors in the background task are now recorded instead of lost.
- **Smaller start-of-session brief: 700 tokens by default, down from 1,000.** On this project's real sessions, simply cutting the budget would have dropped every decision. The brief now shows at most 10 open tasks, so decisions still fit. The other tasks are counted in the "More available" line, and `section="open_tasks"` returns all of them.

### Fixed
- **A session keeps one project name.** The hooks used the folder the agent was in at each event, and that changes when it `cd`s. So one session was briefed as "safetymind", then saved as "cognicore-my-openenv". The project is now the git repository's top-level folder, from any subfolder; linked worktrees count as the main checkout, and a repository at the home folder doesn't count. Outside a repository, it's the folder the session started in, read from the transcript's first `cwd`. Saving a session again never moves it to another project. Existing sessions and project lessons saved under a subfolder are re-keyed once when the database opens. Only the project column changes, and paths that don't exist on this machine, such as synced sessions from other devices, are left as they are.
- **Sarvam summaries no longer invent reasons, mislabel rejected decisions or run out of room.** A real session used 15,865 of the 16,000 output tokens on reasoning, so the limit is now 32,000. A decision's `why` must be one the handoff states: the prompt says so, and a reason whose words mostly aren't in the source is emptied (counted as `dropped_reasons`). A missing reason no longer shows as "(why: None)". A rejected decision stays rejected whether the model marks it with `"status": "rejected"`, a "Rejected -" prefix, or drops the prefix on a decision the handoff already marked rejected.
- **Sentences that state no task are no longer saved as open tasks.** Examples: "you don't need to do anything", "it would need to…", "the URL must be one of those", "compaction must never…", "since both apps must…", "still pending", "that's the next step" and "to-do tool". In this project's saved sessions, these made up 12 of 33 open tasks. Briefs also skip them in sessions saved before this fix.

### Added
- **Project context is suggested, then confirmed.** At the end of a reply, the Stop hook proposes up to three items the session stated, each shown once: a decision with a stated reason, a rule-like sentence the user wrote ("Always…", "Never…", "Don't…", but not "don't worry"), or a goal that differs from the curated one. Run `cognicore project accept 1 2` to add them, `dismiss <n>` to drop them for good, or `suggestions` to see what's waiting. Agents propose through a new local MCP tool, `cognicore_project_propose` (toolset `project`, now on in the plugin with `search,project`); no tool can accept. Items already in the file, or similar to them, aren't suggested. Pending items live in `~/.cognicore/mcp/project_suggestions.json` and are never uploaded. `COGNICORE_SUGGEST=0` turns this off.
- **Project context items carry provenance and stable ids.** Each decision, rule and key value ends with `[id: D3; date: 2026-11-15; by: Anika; session: sess_…]`. `by` is your git `user.name`, or the agent that proposed it. `cognicore project remove D3` removes an item by id, and ids aren't reused. Hand-written items get the next free id when the file is next saved. "Facts" is now called "Key values" (`add value`); files using the old heading still load.
- **Project context: curated facts the brief reads first.** `cognicore project set goal|status "..."`, `add decision "..." --why "..." [--rejected]`, `add rule "..."`, `add fact "..."`, `list`, `remove <kind> <n>`, `history [--diff]` and `path`. They're stored in `.cognicore/context.md` at the repository root, the only store: plain Markdown that can be edited by hand (unknown sections are kept), reviewed in pull requests and rolled back with git. `history` is the file's git log. Secrets are redacted before writing. A repository at the home folder doesn't count, so projects without their own repo don't share one file. The start-of-session brief puts the project context first, capped at 300 tokens (`COGNICORE_PROJECT_TOKENS`) of the 700: goal, status, rules, decisions (newest first), then facts, with a count of what didn't fit. A curated goal replaces the session's guessed one, and session decisions it already states aren't repeated. A project with only a context file, and no saved session yet, still gets a brief. `cognicore usage` counts it in the startup size.
- **`cognicore usage`**: token usage of your Claude Code sessions, read from Claude Code's own transcripts, with no model calls. Per project it shows sessions, turns, input tokens processed, startup context (what the first turn received), final context (what continuing resends every turn), CogniCore's brief size, and the per-turn saving after `/clear`: context − (startup + brief). Options: `--days`, `--json`.

### Performance
- **Hooks start in 0.3 s instead of 1.3 s.** Every hook imported the OpenAI SDK through `cognicore.memory`, which took about a second, and no hook uses it. It now loads on first use.
- **Fewer uploads.** The Stop hook (after every reply) uploads at most once a minute. SessionEnd and PreCompact always upload, and each upload also sends sessions a skipped one left behind.
- **No waiting on a server that's down.** After a network failure, session starts skip the pull for 10 minutes and start from local memory, instead of waiting 3 s for a timeout each time.

### Added
- **AI summaries of synced sessions on the hosted server, with Sarvam AI.** When the server's environment has `SARVAM_API_KEY`, each synced session's handoff is rewritten in the background into a short one: goal, state, decisions, open tasks and key facts. The key lives only on the server. Every item may cite only values present in the original, so invented values are dropped. Exact values, files, code and the Done list are kept as captured. A handoff is summarised once it reaches 3,000 tokens (`COGNICORE_SUMMARY_MIN_TOKENS`), and again only after it grows by as much. In between, the cached summary is reused. The summary is stored just after the upload's revision, so a newer upload always wins and clients pull the summary like any other session. On a test session, 31 open tasks became the 1 real one.

### Fixed
- **A failed sync upload is retried.** Each session was uploaded once, when it was saved. If that upload timed out and the session never saved again (it ended, or the user ran /clear), claude.ai kept the old version. Every upload now also sends any of the 20 most recent sessions the server hasn't accepted yet. Retrying stops at the first network error. A session the server refuses (4xx) is logged and skipped, so it can't block the others.
- **Cursor can sign in to the hosted server.** Cursor registers its desktop callback (`cursor://anysphere.cursor-mcp/oauth/callback`) and its cursor.com callbacks for cloud agents and the bot in one request; the server rejected the whole registration, so Cursor showed "Could not obtain authentication URL". These callbacks are now accepted.

## [0.13.0] — 2026-11-11

### Added
- **`cognicore lesson`**: lessons from mistakes, checked before acting. `add` records the mistake, the fix, the reason and the situation; `check "<task>"` prints at most 3 relevant lessons within about 200 tokens, and nothing when none match; `list` and `forget` review and delete them. Similar lessons are merged and counted, every field is redacted, and lessons stay local in `lessons.db` (SQLite FTS5, BM25).
- **`skills/never-twice/`**: an OpenClaw skill that uses `cognicore lesson` so the agent never repeats a mistake.

### Changed
- The README names the three required dependencies (numpy, jsonschema, requests) instead of claiming none, and asks for a star above the demo.

## [0.12.0] — 2026-11-07

### Added
- **Sign in with Google** on the hosted server, so people without a GitHub account can connect
  CogniCore. It is OpenID Connect with only the `openid` scope: no email, profile or other Google
  data is requested, and no Google token is stored. The ID token is fully verified (Google's keys,
  RS256 only, issuer, audience, authorized party, expiry, nonce) and the account is
  `google:<sub>`, never linked to a GitHub account. Enabled by the optional `GOOGLE_CLIENT_ID` and
  `GOOGLE_CLIENT_SECRET`; the consent page offers Google first, in plain language. See
  "Google OAuth client" in `docs/remote-mcp-server.md`.
- **Automatic deploys of the hosted server** (`deploy/auto-deploy.sh`, `deploy/systemd/`). A timer
  on the VM deploys each new commit on `main` once `.github/workflows/ci.yml` has passed on it,
  health-checks it, and restores the previous commit if the new one does not come up. Changes under
  `deploy/` are held for a person, root runs an installed copy of the script rather than the
  checkout, and the VM pulls, so no inbound access or stored credential is needed.
- `.gitattributes` keeps shell scripts and systemd units LF, so a Windows checkout cannot break them.
- **A deploy/live status on every commit to `main`** (`.github/workflows/deployed.yml`). After CI
  passes it waits for the hosted server to report that commit and marks it live, or explains why
  not (held `deploy/` change, rollback, server unreachable) and notifies whoever merged it.
  `/health` now includes `commit`, built into the image by the deploy, and a deploy fails if the
  server reports a different commit than the one just deployed.

### Changed
- **A cleaner sign-in page.** The Continue buttons now come straight after the headline, so they
  no longer sit below the fold, and what each provider shares is written under its own button.
  Google and GitHub carry equal visual weight. The page uses quieter styling and a short CSS-only
  entrance animation (no scripts, so the strict CSP holds), and the motion turns off under
  `prefers-reduced-motion`. The lead line now reads "No new account or password needed", which
  holds for both providers, replacing a Google-only sentence.
- **An empty account says which sign-in it belongs to.** Google and GitHub sign-ins are separate
  accounts, so reconnecting with the other provider opens an empty one that looks like lost
  memories. When an account holds nothing at all, `cognicore_recall`, `cognicore_list`,
  `cognicore_stats` and `cognicore_resume_session` (text format) now say which provider it
  belongs to and that the other provider's memories are in a separate account. The note names
  providers only, never an account id, and is not added to JSON output.
- **Container logs are deleted after 14 days** (`deploy/logrotate-docker.conf`, installed by
  `deploy/setup-host.sh`). Caddy's access log records visitor IP addresses, and the host kept it
  forever; `PRIVACY.md` now states the 14-day window, and `tests/test_log_retention.py` fails if the
  configuration and the policy drift apart. The compose file also caps each container's live log, so
  a burst cannot fill the disk between daily runs. An existing host needs the file installed once:
  see "Deploying on a VM" in `docs/remote-mcp-server.md`.
- **The privacy policy describes the marketplace accurately.** Using any marketplace tool, including
  read-only ones, creates a profile other users can see, and those profiles, transfer records and
  reputation scores live in one shared database rather than a per-account one. The policy also says
  prices are estimates with no payment taken, that stopping a listing is a request by email, and
  that copies other users already hold are unaffected by deleting your own.
- `cognicore.connectors.oauth.GitHubOAuthServer` is now `OAuthServer`; the old name still works.
- CI on `main` no longer cancels a run when the next merge lands, so every merged commit gets a
  result rather than a "cancelled" failure. Pull requests still cancel superseded runs.
- The resume brief is leaner and fits a token budget. Save replies are compact, and agents can write their own handoff.
- Tool definitions cost about 30% fewer tokens per turn.

### Fixed
- **An automatic deploy took the hosted server down** (2026-11-03). The deploy unit set
  `UMask=0077`, so files root's `git` rewrote became readable by root only; Docker copied that mode
  into the image and the non-root server crash-looped on `Permission denied`. The rollback rewrote
  files the same way, reported success without checking, and the timer retried every 3 minutes.
  Git now runs as the checkout's owner, the umask is gone, a deploy stops before restarting anything
  if a source file is unreadable, a rollback must pass the health check or it reports the server
  down, and a failed commit is not retried until a newer one lands or `--retry` is given.
- **Cross-agent session handoff.** The new `plugins/cognicore` plugin uses hooks to save each Claude Code or Codex session from its transcript, with no model calls. At the next start it resumes the project's latest work as a brief of about 450 tokens. `cognicore install` sets up both agents, and `cognicore setup codex` sets up Codex only.
- **Optional sync with the hosted account.** `cognicore login` signs in through the hosted server's OAuth flow (loopback redirect, PKCE). The hooks then upload each saved session's redacted handoff in the background, and download sessions saved through the connector (claude.ai, Claude Desktop, mobile) at session start, with a 3-second timeout. New ones show as a one-line pointer in the brief. The hosted server adds `PUT/GET /sync/sessions`; a newer revision always wins, so syncs can repeat or arrive out of order. Also adds `cognicore sync`, `cognicore sync status`, `cognicore logout` and `COGNICORE_SYNC=0`.
- **Console scripts** `cognicore-mcp` and `cognicore-hooks`.
- **`COGNICORE_TOOLSETS`** loads only the tool groups you use, on both the local and the hosted server.
- **Read-only memory viewer:** `python -m cognicore.memory.viewer`.
- **Optional AI compression** of long sessions through the Claude API (`COGNICORE_AI_COMPRESS`). It checks the summary against the session, so it can't add facts that weren't there.
- **Benchmarks:** `scripts/bench_transfer.py` runs offline in CI. `scripts/bench_realworld.py` runs head to head against claude-mem; see `docs/transfer-audit.md`.
- **Launch kit and 48-second demo** (Claude Code → claude.ai → Codex, plus measured numbers) in `docs/launch/kit/`, with the Remotion source in `docs/launch/kit/video/`.
- FTS5 search quotes tokens, so hyphens, paths and keywords match.
- Merging near-duplicate sentences no longer drops ones that carry new values.
- A handoff the agent saved partway through a session no longer freezes the session's state. Once the conversation continues past it, the state comes from the newer work and newer tasks are listed first; the saved goal, decisions and facts still count.
- "Doesn't work" describing a limitation ("X doesn't work there") is no longer saved as a rejected approach. Failed attempts still are ("it didn't work", "we tried X but it doesn't work").
- Quoted text no longer decides how a sentence is filed (a sentence quoting "it didn't work" isn't a rejected approach). "Follow-up" marks a task only in task form ("Follow-up: ..."), not in reports like "completed every follow-up". The state is the newest status sentences, up to about 300 characters, with older ones kept as facts.
- The plugin loads only `cognicore_recall` (`COGNICORE_TOOLSETS=search`), since its hooks save and resume: about 230 tokens per turn instead of about 1,240. A new `search` toolset and shorter instructions for hook users make this possible, and a tool listed in several groups is now kept when any of them is requested.
- Statements a long session's caps leave out of the handoff are stored locally as searchable rows, so `cognicore_recall` finds them while briefs and sync stay short.
- The hosted `cognicore_recall` searches all of your memories by default (`scope="all"`). Before, it searched only `user` scope, so facts from saved or synced sessions were never found.
- The local `cognicore_recall` no longer labels saved facts and notes `[FAILURE]`; success and failure are shown only for experiences, which record an outcome.
- Sessions saved with the local `cognicore_save_session` tool get a sync id and upload in the background when sync is on.
- Long sessions no longer flood the handoff. Table rows, list lead-ins ("Next steps:") and questions are no longer saved as tasks or decisions. A session keeps at most 25 decisions, 25 tasks and 40 key facts, preferring recent ones and those with exact values; exact values and files are never capped.
- Deploy configs are hardened against common 502 causes, such as keep-alive and health-check timeouts.

## [0.11.0] — 2026-11-01

### Added
- **Hosted MCP server** (`cognicore.connectors.remote`), running at `https://mcp.cognicoree.tech/mcp/stream` (Streamable HTTP) and `/mcp/sse`. Each signed-in user gets their own SQLite memory store.
- **OAuth 2.1 sign-in through GitHub** (`cognicore.connectors.oauth`): dynamic client registration, PKCE (S256), rotating refresh tokens, and RFC 8414 / RFC 9728 metadata, so MCP clients connect without manual setup. Works with the Claude and ChatGPT connectors (including per-connector ChatGPT callback URLs). Other clients are allowed through `COGNICORE_EXTRA_REDIRECT_URIS`.
- **Tool titles and behaviour annotations** on all 31 hosted MCP tools (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), so clients know which calls change data.
- `server.json` for the MCP registry, published as `io.github.cognicore-dev/cognicore-env`.
- **mem0 Integration (`cognicore.integrations.mem0`)**: Verified memory transfer bridge to mem0 (`mem0ai/mem0`). Sealed bundle export/import with Ed25519 signing, deterministic canonical JSON, fail-closed signature verification, consequence-class split (authority vs information), structural quarantine (`quarantine.json` with dedicated TF-IDF index and query path), custody chain revocation checking, environment compatibility, promotion requiring fresh verification evidence, and index recall reachability assertions. Includes `sync_to_mem0()` for pushing verified memories into a live mem0 client. See `cognicore/integrations/mem0/` and `tests/test_mem0_bridge.py`.
- **Experience transfer** (`cognicore.experience`): verification, revalidation, compatibility checks and conflict handling for reusing past experiences across projects.
- **Probabilistic Cognitive Control** (`cognicore.decision`): safety gate and decision logging.
- **Deployment for the hosted server** (`deploy/`): Docker Compose, a Caddy reverse proxy that
  terminates TLS and sets security headers, and `setup-host.sh` to prepare a fresh host.
  `docs/remote-mcp-server.md` covers the setup. (#175)
- **Nightly off-host backup** (`deploy/backup-to-blob.sh`): archives each user's database to object
  storage on a schedule, using a write-only credential so a compromised host cannot read or delete
  existing backups. (#176)
- **Reference for all 31 hosted MCP tools** (`docs/mcp-tools.md`), stating for each tool whether it
  reads data, changes data, or calls a third-party service.

### Changed
- **Repository restructured into subpackages** (`memory`, `agents`, `safety`, `evaluation`, `training`, `observability`, `connectors`, `experimental`, …). Every old import path still works and resolves to the same module; see `cognicore/_compat.py`. New code should use the new paths.
- `SQLiteMemoryBackend` lexical search scores are now bounded to (0, 1], with the best match scoring 1.0, matching the range the hybrid backend already used. Ranking is unchanged. (#148)
- Optional tool arguments accept an explicit `null`. (#152)
- Memory extraction uses fewer tokens.
- **The hosted server image is hardened** (#176): base images pinned by digest, dependencies
  installed from a hash-pinned `deploy/requirements.txt`, and the container runs as a non-root user
  on a read-only root filesystem with capabilities dropped, writing only to `/data` and `/tmp`.
  Dependabot now keeps the pinned deployment dependencies updated.
- **Host hardening** (#176): key-only SSH with root login refused, and fail2ban rate-limiting
  connection attempts.
- `HOME` is set to `/tmp` in the container image so the package can import on the read-only
  filesystem. User data is stored under `COGNICORE_DATA_DIR`; anything written to the home directory
  does not survive a restart. (#176)

### Fixed
- **Security:** on the hosted server, one user could buy or receive another user's memories through the marketplace and sharing tools. `cognicore_purchase_memory` now requires the seller to have listed their memories, and `cognicore_share_experience` refuses to write into another account. (#161)
- **Security:** removed hardcoded credentials and runtime artifacts from the repository.
- Fabric tools on the hosted server called an undefined function and failed on every call.
- The container image's default command started the undeployed ChatGPT connector, so running the
  image without `deploy/docker-compose.yml` served the wrong application. It now starts the
  hosted server (`cognicore.connectors.remote`), which is what the compose file already ran.
- `plugins/cognicore-experience/.mcp.json` was missing its `mcpServers` wrapper, so the plugin
  defined no MCP server and started nothing.
- **Security:** the ChatGPT connector no longer falls back to a hardcoded Supabase project URL. The project it named has since been deleted, so a deployment that did not set `SUPABASE_URL` pointed its OAuth metadata, sign-in redirect and token exchange at a dead host instead of failing. `SUPABASE_URL` is now required and read at request time.
- Hosted tool descriptions no longer tell the model when to call a tool or name other tools, which the Claude connector directory rejects and which is a prompt-injection surface, because clients pass these strings straight to the model.
- `import_bundle` in the mem0 bridge now flushes the quarantine backend, so quarantined entries are not lost. (#151)
- `cognicore.integrations.elevenlabs` imports again after the module moved to `cognicore.fabric.plugins.elevenlabs`. (#113)
- Undefined names in type hints in `decision/logging.py`, `fabric/engine.py` and `realbench/coding_bench.py`.
- A test keeps the version in `pyproject.toml`, `cognicore.__version__` and `server.json` in sync.
- PyPI project links point at `cognicore-dev/cognicore-env`.
- **Security:** the hosted server rate-limits requests, including unauthenticated ones by client IP
  from `X-Real-IP`, and no longer accepts access tokens outside request headers. Failed
  authentication returns a terse 401 that does not describe why it failed. (#173)

## [1.0.0] - 2026-07-06

### Added
- **AgentPassport (`cognicore.passport`)**: Universal serialization wrapper (zip/json/npz) for packaging agents.
- **AgentDNA (`cognicore.dna`)**: Behavioral genome extraction and evolutionary algorithms with SVG visualization.
- **Conscience (`cognicore.conscience`)**: Real-time self-auditing, uncertainty detection, and decision holding.
- **Civilization (`cognicore.civilization`)**: Federated agent behavioral knowledge sharing and server node.
- **TimeTraveler (`cognicore.timetravel`)**: State rewinding, counterfactual branching, and timeline comparison.
- **Oracle (`cognicore.oracle`)**: Predictive simulation, adversarial search, and goal-directed hallucinations via environment models.
- **DreamEngine (`cognicore.dream`)**: Synthetic experience generation, adversarial nightmares, and hallucinatory rollouts.
- **CLI Enhancements**: Added `passport` and `civilization` management commands to the central CLI.

### Changed
- Refactored core modules to prepare for the "Living Agent Stack".


## [0.9.3] — 2026-06-18

### Changed
- **MemoryBackend ABC migration complete** — `get_context()` → `get_by_category()`, `retrieve()` → `get_by_category()` across all backends and callers
- Fixed 15 test failures caused by interface migration
- All **525 tests passing**

### Fixed
- Prompt injection pattern improvements in SafetyMonitor
- Smoothed IDF computation in TFIDFMemoryBackend for stability with small corpora

---

## [0.9.2] — 2026-06-11

### Added
- **PyPI release** with full benchmark report

### Changed
- Memory benchmark results: **12.2% solve rate** vs 1.1% baseline (11× improvement)

---

## [0.9.1] — 2026-06-10

### Fixed
- 4 remaining bug fixes from 0.9.0 release
- Benchmark report corrections and README rewrite

---

## [0.9.0] — 2026-06-09

### Fixed
- 8 memory and reflection bug fixes across `TFIDFMemoryBackend`, `ReflectionEngine`, and `RewardBuilder`

### Changed
- 4 performance optimizations in memory retrieval and reflection scoring
- **TFIDFMemoryBackend** established as the default backend for all runtime paths

---

## [0.8.3] — 2026-06-05

### Fixed
- AutoCurriculum task generation edge cases
- `Experiment.learn()` convergence issues with small episode counts

---

## [0.8.2] — 2026-06-03

### Fixed
- AutoLearner initialization when no prior episodes exist
- SafetyLayer false-positive detection in edge cases
- `episodes=0` parameter handling across train/evaluate APIs

---

## [0.8.1] — 2026-06-01

### Fixed
- Memory retrieval returning stale entries after backend swap
- Replay persistence corruption on interrupted writes
- Branch tracking state desync in multi-agent scenarios

---

## [0.8.0] — 2026-05-28

### Added
- **NEXUS Live Runtime Dashboard** with 9 real subsystems:
  - Memory Explorer, Reflection Viewer, Safety Monitor, Reward Tracker
  - Episode Timeline, Agent Health, Middleware Pipeline, Config Editor, Logs
- **Agent Immune System** (`cognicore/immune/`) — anomaly detection and adaptive response for agent behavior
- **Replay Time Travel** (`cognicore/replay/`) — rewind, branch, and replay agent episodes with RL integration

---

## [0.7.0] — 2026-05-20

### Added
- **NEXUS Autonomous Runner** — Devin-like engine for end-to-end task execution
- **Enterprise integrations:**
  - GitHub (PR creation, issue management)
  - CI pipeline orchestration
  - Slack notifications
  - Linear issue tracking
  - Scheduler for recurring tasks
- **Multi-agent runtime** with 4 routing policies:
  - Round-robin, capability-based, load-balanced, priority-queue

---

## [0.6.0] — 2026-05-10

### Added
- **Research features:**
  - `FailurePredictor` — predict agent failure before it happens
  - `CognitiveMemory` — biologically-inspired memory consolidation
  - `RedVsBlue` — adversarial agent evaluation framework
  - `AIDebugger` — automated agent behavior debugging
  - `IntelligenceScorer` — multi-dimensional agent capability scoring
  - `ThoughtTracer` — agent reasoning chain visualization
  - `KnowledgeTransfer` — cross-agent knowledge sharing
  - `EvolutionEngine` — evolutionary strategy optimization for agents
- **SWE-bench runner** with **20/20 resolve rate** on test suite

---

## [0.5.0] — 2026-05-01

### Added
- **Real-world safety environment** — `RealWorldSafety-v1` with 30 curated cases:
  - Jailbreak attempts (DAN, roleplay, character-based)
  - PII leakage (SSN, credit cards, addresses)
  - Prompt injection (translation, summarization attacks)
  - Hate speech, self-harm, illegal activity detection
  - Medical/legal advice boundary cases
  - Tricky safe edge cases ("kill the process", "fire an employee")
- **Real-world code bugs environment** — `RealWorldCodeBugs-v1` with 18 production Python bugs:
  - SQL injection, race conditions, resource leaks
  - Mutable default arguments, closure bugs, bare excepts
  - Hardcoded secrets, async antipatterns, type errors
- **6 new environment variants** — Easy/Medium/Hard for both real-world envs
- **30 total environments** (up from 24)

### Changed
- Fixed PyPI URLs (previously pointed to non-existent `cognicore/cognicore` repo)
- Added `CHANGELOG.md` with full version history
- Updated roadmap with dated milestones

---

## [0.4.0] — 2026-04-30

### Added
- **Custom error hierarchy** — 7 exception classes with actionable messages and suggestions
  - `InvalidEnvironmentError` — shows similar env names ("Did you mean?")
  - `InvalidConfigError` — catches bad config on construction
  - `AgentInterfaceError` — clear message when agent lacks `act()`
  - `EpisodeFinishedError` — replaces silent failure on double-step
- **AgentProtocol** — runtime-checkable Protocol for duck-typing agent validation
- **Config validation** — `CogniCoreConfig.__post_init__()` validates all fields immediately

### Changed
- **Replaced 300+ `print()` calls with `logging`** across 31 modules
- **Type-safe API** — `train()` and `evaluate()` validate agent/env/episodes before running
- `make()` now raises `InvalidEnvironmentError` instead of generic `KeyError`
- `step()` raises `EpisodeFinishedError` instead of silently returning empty data

---

## [0.3.0] — 2026-04-28

### Added
- **CLI commands** — `cognicore train`, `cognicore demo`, `cognicore metrics`
- **Config-driven training** — YAML config files (`configs/default.yaml`, `configs/strict_safety.yaml`)
- **Deterministic benchmarks** — 5 seeds × 10 episodes, mean ± std dev, saved JSON reports
- **Real-world use case** — `examples/chatbot_safety_eval.py` (chatbot safety evaluation)
- **Learning curve graph** — `docs/learning_curve.png` embedded in README
- **README overhaul** — tagline, before/after output, comparison table, how-it-works diagram
- **Known limitations section** — 5 honest limitations documented
- **Roadmap** — plugin ecosystem vision (cybersec, finance, eval)
- `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md`

---

## [0.2.0] — 2026-04-15

### Added
- **24 environments** across 6 domains (safety, math, code, conversation, planning, summarization)
- **`cc.train()` / `cc.evaluate()`** — clean 2-line API
- **22 CLI commands** — `cognicore list`, `run`, `benchmark`, `serve`, `dashboard`, etc.
- **Structured Rewards** — 8-component reward signal per step
- **PROPOSE → Revise protocol** — tentative exploration before commitment
- **Safety Monitor** — streak detection and health status
- **Gymnasium adapter** — `CogniCoreGymAdapter` for RL compatibility
- **API server** — FastAPI-based REST API
- **GitHub Actions CI** — tests on Python 3.9/3.11/3.12, linting, security scan

---

## [0.1.0] — 2026-04-05

### Added
- Initial release
- Core `CogniCoreEnv` base class with Memory, Reflection, and Structured Rewards
- `SafetyClassification-v1` environment
- Basic agent interface (`BaseAgent`, `RandomAgent`)
- `cognicore.make()` factory function

---

[0.9.3]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.8.3...v0.9.0
[0.8.3]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Kaushalt2004/cognicore-my-openenv/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Kaushalt2004/cognicore-my-openenv/releases/tag/v0.1.0

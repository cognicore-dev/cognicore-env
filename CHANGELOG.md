# Changelog

All notable changes to CogniCore NEXUS are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

## [0.11.0] — 2026-10-30

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

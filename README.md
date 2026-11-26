# CogniCore

<!-- mcp-name: io.github.cognicore-dev/cognicore-env -->

**Local-first session memory for Claude Code and Codex**

Start a fix in Claude Code. Open Codex in the same project and it can pick up the ticket, the reasoning behind your last change, and the test you still need to write. CogniCore saves that handoff locally without a model API key.

[![PyPI](https://img.shields.io/pypi/v/cognicore-env)](https://pypi.org/project/cognicore-env/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/cognicore-env/)
[![Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

[Quickstart](#quickstart) · [How it works](#how-it-works) · [Integrations](#integrations) · [Python SDK](#python-sdk) · [Benchmarks](#benchmarks)

## Quickstart

With Python 3.10+ and either Claude Code or Codex installed, run:

```bash
pipx install "cognicore-env[mcp]"
cognicore install
```

1. Restart Claude Code or Codex. In Codex, run `/hooks` and trust the CogniCore hooks.
2. Work in a project and leave a concrete follow-up, such as “The CSV export handles empty rows. Next, add a regression test for trailing blank lines.” End the session.
3. Open a new session from the same project directory, in either agent. Ask “What should I do next?” The startup brief should include the follow-up and the work that led to it.

If you use uv, install the CLI with `uv tool install "cognicore-env[mcp]"`. Use `cognicore install --agent codex` to set up just Codex, or `cognicore install --dry-run` to preview the installer. [Claude Code marketplace setup and hook configuration](plugins/cognicore/README.md).

[![Demo showing a Claude Code session handing off work to Codex](docs/launch/kit/cognicore-demo.gif)](docs/launch/kit/cognicore-demo.mp4)

## How it works

1. **Capture.** At the end of a coding session, hooks extract decisions, open tasks, facts, and exact values such as ticket IDs and file paths. The default extractor uses rules. Model-based compression is optional.
2. **Resume.** When you reopen the project, the startup hook supplies a brief with a default budget of 700 tokens.
3. **Recall.** The agent can use `cognicore_recall` to look up details left out of the brief.

Local session memory lives in SQLite under `~/.cognicore/mcp`. Capture uses conversation text and edited file paths, and redacts detected secrets before saving. Open the read-only session viewer with `python -m cognicore.memory.viewer` from a Python environment where CogniCore is installed. [Storage, capture, and configuration](plugins/cognicore/README.md).

CogniCore groups sessions by Git repository, including its subfolders and linked worktrees. Outside a repository, it uses the folder where the session started. A repository rooted in your home directory is ignored.

### Keep project decisions in Git

Save standing decisions and rules in `.cognicore/context.md`. CogniCore puts curated context first in the startup brief, and your team can review changes in pull requests.

```bash
cognicore project set goal "Make CSV exports reliable for large reports"
cognicore project add decision "Stream rows" --why "Keep memory usage bounded"
cognicore project add rule "Test empty exports and trailing blank lines"
cognicore project list
```

Run `cognicore project suggestions` to review proposals captured during sessions and `cognicore project accept <id>` to add one to the file.

## Import historical conversations

Import a ChatGPT ZIP or JSON export to search original messages, reconstruct project decisions, and trace claimed outcomes back to their source. Historical records keep their original event times and live in a private local archive. [Retrospective Memory Engine tutorial](docs/history.md)

```bash
cognicore history import chatgpt-export.zip --consent --project DIPY
cognicore history search "DIPY optimization" --project DIPY
cognicore history timeline --project DIPY --kind decision
```

To sync your stored archive across devices, run `cognicore history sync enable --consent` once on the device holding it. Other devices with empty archives restore automatically after you sign in to the same account. [History sync setup](docs/history.md#sync-history-across-devices)

## Integrations

| If you use | Setup | How memory is accessed |
| --- | --- | --- |
| Claude Code or Codex | [Install the hooks](#quickstart) | Automatic capture and resume using a shared local store |
| Claude on web, desktop, or mobile | [Set up the hosted connector](#optional-hosted-sync) | Access hosted memory through the connector |
| Another MCP client | [Configure the local server](#local-mcp) or add the hosted endpoint | Use the memory tools from your client |
| Your own Python agent | [Install the SDK](#python-sdk) | Store and retrieve memory directly in your code |

### Optional hosted sync

Add `https://mcp.cognicoree.tech/mcp/stream` as a custom MCP connector in a compatible client and sign in with GitHub. To link your local Claude Code or Codex memory, run `cognicore login` and sign in with the same account. Sync sends the redacted handoff and can pull sessions saved through the connector.

Use `cognicore sync status` to check your connection and `cognicore sync` to sync manually. [Hosted MCP and self-hosting guide](docs/remote-mcp-server.md).

### Local MCP

For clients that accept a stdio MCP configuration, add this entry with `uvx` available on your PATH:

```json
{
  "mcpServers": {
    "cognicore": {
      "command": "uvx",
      "args": ["--from", "cognicore-env[mcp]", "cognicore-mcp"]
    }
  }
}
```

The [hosted MCP tool reference](docs/mcp-tools.md) lists the tools available through the hosted connector and their side effects.

## Python SDK

```bash
pip install cognicore-env
```

Give an agent a searchable SQLite store for what worked, what failed, and why. This example opens the same database twice to show how a later run can recall an earlier result.

```python
from cognicore.memory.base import MemoryEntry
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend

memory = SQLiteMemoryBackend("agent-memory.db")
memory.store(
    MemoryEntry(
        text="CSV export skips trailing blank lines when rows are streamed.",
        category="csv-export",
        correct=True,
    )
)

# A later run can open the same database.
memory = SQLiteMemoryBackend("agent-memory.db")
for result in memory.search("CSV export", top_k=3):
    print(result.entry.text)
```

SQLite search works with the base package. The `[memory]` extra adds dependencies for embedding-based retrieval, and `[mem0]` adds dependencies for signed memory transfer bundles. See the [Python API](docs/api_reference.md), [runnable examples](examples/README.md), and [experience transfer guide](docs/experience-transfer.md) for reflection, replay, and verified memory sharing.

## Benchmarks

### Coding-session handoff

In the November 1, 2026 audit, both CogniCore and Claude-Mem 15.7.0 completed all three deferred follow-ups in each route. The harness withheld the prior transcript and simulated both products’ Codex startup hooks by running their hook commands and passing their output to Codex.

| Handoff | Memory system | New tokens | Memory ready | Recall |
| --- | --- | ---: | ---: | ---: |
| Claude Code → Claude Code | CogniCore | 52,609 | ~1 s | 24/24 |
| Claude Code → Claude Code | Claude-Mem | 215,888 | 70–80 s | 24/24 |
| Claude Code → Codex | CogniCore | 60,310 | ~1 s | 23/24 |
| Claude Code → Codex | Claude-Mem | 146,520 | 80–130 s | 24/24 |

New token counts include background processing. The audit covers three tasks per route and one run per task. [Method, scoring notes, and full results](docs/transfer-audit.md#head-to-head-with-claude-mem-on-current-versions-2026-11-01).

### LongMemEval retrieval

On the repository’s LongMemEval evaluation, multi-hop retrieval reached **85.2% strict recall with five chunks** versus **78.8%** for the zero-shot baseline. Both reached 95.0% at 20 chunks. [Evaluation setup, results, and reproduction steps](docs/benchmarks.md).

## Documentation and support

- [CLI and configuration](plugins/cognicore/README.md) · [Architecture](docs/architecture.md) · [Integrations guide](docs/integrations.md)
- [Contributing](CONTRIBUTING.md) · [Report an issue](https://github.com/cognicore-dev/cognicore-env/issues) · [Project website](https://www.cognicoree.tech)

## Launches

<p>
  <a href="https://www.producthunt.com/products/cognicore/launches/cognicore-2?embed=true&amp;utm_source=badge-featured&amp;utm_medium=badge&amp;utm_campaign=badge-cognicore-2"><img src="https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=1284310&amp;theme=light&amp;t=1795502699738" alt="CogniCore on Product Hunt" width="250" height="54"></a>
  <a href="https://peerlist.io/kaushalt268/project/cognicore--memory-that-follows-you-between-ai"><img src="docs/launch/kit/peerlist-launch.png" alt="CogniCore on Peerlist Launchpad" width="250" height="131"></a>
</p>

## Program membership

<a href="https://www.nvidia.com/en-us/startups/"><img src="docs/launch/kit/nvidia-inception.png" alt="NVIDIA Inception Program" width="250"></a>

## License

[Apache License 2.0](LICENSE). Built by [Kaushal Trivedi](https://www.linkedin.com/in/kaushal-trivedi-4450a8265), [Durvesh Bhadgaonkar](https://www.linkedin.com/in/durvesh-bhadgaonkar), and [Hassaan Tole](https://www.linkedin.com/in/hassaan-tole/).

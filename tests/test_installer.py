"""`cognicore install` and the local-first cognicore plugin (no real agents are touched)."""
import json
from pathlib import Path

import pytest

from cognicore import installer

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fake_agents(monkeypatch, tmp_path):
    calls = []

    class Proc:
        returncode, stdout, stderr = 0, "ok", ""

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Proc()

    tools = {"claude": "/bin/claude", "codex": "/bin/codex", "cognicore-mcp": "/bin/cognicore-mcp",
             "cognicore-hooks": "/bin/cognicore-hooks"}
    monkeypatch.setattr(installer.shutil, "which", lambda name: tools.get(name))
    monkeypatch.setattr(installer.subprocess, "run", run)
    home = tmp_path / "codex"
    home.mkdir()
    return calls, home, tools


def test_install_sets_up_both_agents(fake_agents):
    calls, home, _ = fake_agents
    out = []
    assert installer.install(codex_home=str(home), out=out.append) == 0
    assert ["/bin/claude", "plugin", "marketplace", "add", "cognicore-dev/cognicore-env"] in calls
    assert ["/bin/claude", "plugin", "install", "cognicore@cognicore-beta-marketplace"] in calls
    assert ["/bin/codex", "mcp", "add", "cognicore", "--env", "COGNICORE_TOOLSETS=memory,session", "--",
            "/bin/cognicore-mcp"] in calls
    assert (home / "hooks.json").exists() and "cognicore:begin" in (home / "AGENTS.md").read_text(encoding="utf-8")
    assert any("/hooks" in line for line in out)


def test_dry_run_changes_nothing(fake_agents):
    calls, home, _ = fake_agents
    out = []
    installer.install(dry_run=True, codex_home=str(home), out=out.append)
    assert calls == [] and not (home / "hooks.json").exists()
    assert sum(line.startswith("  would:") for line in out) == 4


def test_existing_codex_server_is_kept_and_single_agent(fake_agents):
    calls, home, _ = fake_agents
    (home / "config.toml").write_text('[mcp_servers.cognicore]\ncommand = "python"\n', encoding="utf-8")
    out = []
    installer.install(agents=["codex"], codex_home=str(home), out=out.append)
    assert not any(c[:2] == ["/bin/codex", "mcp"] for c in calls)
    assert not any("claude" in c[0] for c in calls)
    assert any("kept it" in line for line in out)


def test_missing_agents_are_explained(fake_agents, monkeypatch, tmp_path):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    out = []
    installer.install(codex_home=str(tmp_path / "no-codex"), out=out.append)
    text = "\n".join(out)
    assert "/plugin install cognicore@cognicore-beta-marketplace" in text and "Codex not found" in text


def test_failed_step_is_reported(fake_agents, monkeypatch):
    _, home, _ = fake_agents

    class Bad:
        returncode, stdout, stderr = 1, "", "marketplace unreachable"

    monkeypatch.setattr(installer.subprocess, "run", lambda cmd, **kw: Bad())
    out = []
    assert installer.install(agents=["claude"], codex_home=str(home), out=out.append) == 1
    assert any("FAIL" in line and "marketplace unreachable" in line for line in out)


def test_local_plugin_manifest():
    plugin = ROOT / "plugins" / "cognicore"
    meta = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert meta["name"] == "cognicore"
    server = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["cognicore"]
    assert server["command"] == "cognicore-mcp" and server["env"]["COGNICORE_TOOLSETS"] == "search"
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "Stop", "PreCompact", "SessionEnd"}
    for groups in hooks.values():
        for h in groups[0]["hooks"]:
            assert "cognicore-hooks" in h["command"] and "-m cognicore.hooks" in h["command"]
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert {"cognicore", "cognicore-memory"} <= {p["name"] for p in market["plugins"]}
    assert not (ROOT / "plugins" / "cognicore-memory" / "hooks" / "hooks.json").exists()  # hooks live in the local plugin


def test_console_scripts_declared():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'cognicore-mcp = "cognicore.mcp.server:main"' in text and 'cognicore-hooks = "cognicore.hooks:main"' in text

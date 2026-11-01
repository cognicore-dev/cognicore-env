"""What ships must point at what actually runs.

Two things broke quietly here before: the image's default command started a
connector that is not deployed, and a plugin's MCP config was missing the key
Claude Code reads, so it defined no server at all. Both parse fine and fail
only at run time, so they are pinned here.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_image_default_command_starts_the_hosted_server():
    cmd = re.search(r"^CMD (\[.*\])", (ROOT / "Dockerfile").read_text(encoding="utf-8"), re.MULTILINE)
    assert cmd, "Dockerfile has no CMD"
    argv = json.loads(cmd.group(1))
    assert "cognicore.connectors.remote:app" in argv, (
        f"the image should default to the deployed server, not {argv}"
    )


def test_compose_runs_the_same_application_as_the_image():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "cognicore.connectors.remote:app" in compose


def test_every_plugin_declares_its_mcp_servers():
    """Claude Code reads servers from the "mcpServers" key; without it, nothing starts."""
    configs = sorted((ROOT / "plugins").glob("*/.mcp.json"))
    assert configs, "no plugin MCP configs found"
    for path in configs:
        config = json.loads(path.read_text(encoding="utf-8"))
        servers = config.get("mcpServers")
        assert servers, f"{path.parent.name}/.mcp.json declares no mcpServers"
        for name, server in servers.items():
            assert "command" in server or "url" in server, (
                f"{path.parent.name} server {name!r} has neither command nor url"
            )


def test_marketplace_entries_point_at_real_plugins():
    marketplace = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    for entry in marketplace["plugins"]:
        source = (ROOT / entry["source"]).resolve()
        assert source.is_dir(), f"{entry['name']} points at a missing directory: {source}"
        assert (source / ".claude-plugin" / "plugin.json").is_file(), (
            f"{entry['name']} has no plugin manifest"
        )

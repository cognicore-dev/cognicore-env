"""cognicore setup codex: one idempotent, removable block in Codex's AGENTS.md."""
from cognicore.agent_setup import BEGIN, END, setup_codex


def test_setup_codex_adds_once_keeps_user_text_and_removes(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# My rules\nAlways use tabs.\n", encoding="utf-8")

    assert "Added" in setup_codex(str(tmp_path))
    first = agents.read_text(encoding="utf-8")
    assert first.startswith("# My rules\nAlways use tabs.\n") and first.count(BEGIN) == 1
    assert "cognicore_resume_session" in first and "cognicore_save_session" in first

    assert "already has" in setup_codex(str(tmp_path))
    assert agents.read_text(encoding="utf-8") == first

    assert "Removed" in setup_codex(str(tmp_path), remove=True)
    assert agents.read_text(encoding="utf-8") == "# My rules\nAlways use tabs.\n"
    assert "nothing to remove" in setup_codex(str(tmp_path), remove=True)


def test_setup_codex_creates_file_and_honours_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    setup_codex()
    text = (tmp_path / "home" / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith(BEGIN) and text.rstrip().endswith(END)


def test_servers_send_actionable_instructions():
    import pytest

    pytest.importorskip("mcp")
    from cognicore.mcp.server import MEMORY_INSTRUCTIONS, create_mcp_server

    assert create_mcp_server().instructions == MEMORY_INSTRUCTIONS
    assert "cognicore_resume_session" in MEMORY_INSTRUCTIONS
    remote = pytest.importorskip("cognicore.connectors.remote")
    assert remote.mcp.instructions == MEMORY_INSTRUCTIONS

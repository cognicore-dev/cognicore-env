"""The Stop hook tells the user when a session got expensive, so /clear + the brief takes over."""
import json

import pytest

from cognicore import hooks


def _transcript(tmp_path, *contexts):
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})]
    for n in contexts:
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "ok", "usage": {
            "input_tokens": 4, "cache_read_input_tokens": n - 1004, "cache_creation_input_tokens": 1000}}}))
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("COGNICORE_CLEAR_AT", raising=False)


def test_context_size_comes_from_the_latest_usage(tmp_path):
    assert hooks.context_tokens(_transcript(tmp_path, 5_000, 120_000)) == 120_000


def test_a_small_session_gets_no_tip(tmp_path):
    assert hooks.clear_hint(_transcript(tmp_path, 40_000), "claude-code:s1") == ""


def test_a_large_session_gets_one_tip_then_again_only_after_it_grows(tmp_path):
    first = json.loads(hooks.clear_hint(_transcript(tmp_path, 100_000), "claude-code:s1"))
    assert "100k tokens" in first["systemMessage"] and "/clear" in first["systemMessage"]
    assert hooks.clear_hint(_transcript(tmp_path, 120_000), "claude-code:s1") == ""  # not grown enough
    assert hooks.clear_hint(_transcript(tmp_path, 160_000), "claude-code:s1") != ""  # 1.5x bigger
    assert hooks.clear_hint(_transcript(tmp_path, 100_000), "claude-code:s2") != ""  # other session


def test_the_tip_can_be_turned_off_or_moved(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_CLEAR_AT", "0")
    assert hooks.clear_hint(_transcript(tmp_path, 500_000), "claude-code:s1") == ""
    monkeypatch.setenv("COGNICORE_CLEAR_AT", "30000")
    assert hooks.clear_hint(_transcript(tmp_path, 40_000), "claude-code:s1") != ""


def test_no_usage_recorded_means_no_tip(tmp_path):
    path = tmp_path / "codex.jsonl"
    path.write_text(json.dumps({"type": "message", "content": "x"}), encoding="utf-8")
    assert hooks.clear_hint(str(path), "codex:s1") == ""


def test_after_clear_the_brief_is_injected(tmp_path, monkeypatch):
    """/clear starts a session with source "clear": the brief must come back."""
    from cognicore.memory.context_preservation import save_session

    save_session(hooks._backend(), [{"role": "user", "content": "Our goal is to ship 0.14.0 of cognicore-env."}],
                 project=str(tmp_path))
    out = hooks.session_start({"cwd": str(tmp_path), "source": "clear"})
    assert "0.14.0" in json.loads(out)["hookSpecificOutput"]["additionalContext"]

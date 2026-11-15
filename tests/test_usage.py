"""cognicore usage: token usage read from Claude Code transcripts."""
import json

import pytest

from cognicore import usage


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")


def _turn(msg_id, fresh, cache_read, cache_write, out=50, sidechain=False):
    return {"type": "assistant", "cwd": "/work/billing", "timestamp": "2026-11-15T10:00:00Z",
            "isSidechain": sidechain, "message": {"id": msg_id, "role": "assistant", "usage": {
                "input_tokens": fresh, "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write, "output_tokens": out}}}


def _write(tmp_path, name, entries):
    folder = tmp_path / "claude" / "projects" / "-work-billing"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def test_session_usage_counts_each_turn_once_and_skips_subagents(tmp_path):
    path = _write(tmp_path, "s1", [
        {"type": "user", "cwd": "/work/billing", "message": {"role": "user", "content": "hi"}},
        _turn("m1", 4, 0, 40_000),
        _turn("m1", 4, 0, 40_000, out=120),  # the same message, streamed in parts
        _turn("sub", 3, 0, 9_000, sidechain=True),
        _turn("m2", 10, 40_000, 2_000),
        _turn("m3", 10, 42_000, 8_000),
    ])
    s = usage.read_session(path)
    assert s["project"] == "/work/billing" and s["turns"] == 3
    assert s["startup"] == 40_004 and s["context"] == 50_010
    assert s["input"] == 40_004 + 42_010 + 50_010
    assert s["output"] == 120 + 50 + 50


def test_projects_report_startup_context_and_the_saving_after_clear(tmp_path, monkeypatch):
    _write(tmp_path, "s1", [_turn("a", 0, 0, 40_000), _turn("b", 0, 40_000, 60_000)])
    _write(tmp_path, "s2", [_turn("c", 0, 0, 40_000), _turn("d", 0, 40_000, 20_000)])
    monkeypatch.setattr(usage, "brief_tokens", lambda project: 700)
    rows = usage.by_project(usage.collect(days=1))
    assert len(rows) == 1
    r = rows[0]
    assert (r["sessions"], r["avg_startup"], r["avg_context"]) == (2, 40_000, 80_000)
    assert r["saving_per_turn"] == 80_000 - 40_000 - 700
    text = usage.render(rows, 1)
    assert "billing" in text and "39k" in text


def test_no_brief_means_no_claimed_saving(tmp_path):
    _write(tmp_path, "s1", [_turn("a", 0, 0, 40_000), _turn("b", 0, 40_000, 60_000)])
    rows = usage.by_project(usage.collect(days=1))  # nothing saved in CogniCore for this project
    assert rows[0]["brief"] == 0 and rows[0]["saving_per_turn"] == 0


def test_no_transcripts_is_a_plain_message():
    assert "No Claude Code sessions" in usage.render(usage.by_project(usage.collect(days=1)), 1)

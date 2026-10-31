"""Automatic capture: Claude Code transcript parsing and the SessionStart / save hooks."""
import io
import json
import sqlite3

import pytest

from cognicore import hooks
from cognicore.memory.transcript import read_transcript


def _line(kind, content, **extra):
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}, **extra})


def _write_transcript(path, cwd, extra_lines=()):
    lines = [
        json.dumps({"type": "api-request-blob", "content": [{"type": "text", "text":
                    "<system-reminder>The user's email address is someone@example.com</system-reminder>"}]}),
        _line("user", "<system-reminder>context injected by the harness</system-reminder>"
                      "Ticket BILL-311: totals are off by one cent in Germany. We decided to use Decimal with "
                      "ROUND_HALF_UP because finance requires commercial rounding. TODO next session: make "
                      "format_eur return '12,34 €'. The reviewer is Anika and the API key is "
                      "sk-test-FAKE0000000000000000000000."),
        _line("assistant", [
            {"type": "thinking", "thinking": "private reasoning that must not be stored"},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": f"{cwd}/billing.py", "old_string": "a"}},
            {"type": "tool_use", "name": "mcp__cognicore__cognicore_save_session",
             "input": {"handoff_json": json.dumps({"key_facts": ["Staging deploy is on Thursday"]})}},
            {"type": "text", "text": "**Fixed** total_with_tax in billing.py; tests pass."},
        ]),
        _line("user", [{"type": "tool_result", "content": "SECRET FILE CONTENTS"}]),
        _line("user", "=== COGNICORE CONTEXT RESUME ===\nGoal: old goal\n=== END CONTEXT | 9 tokens ==="),
        _line("user", "meta prompt", isMeta=True),
        *extra_lines,
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_read_transcript_keeps_what_was_said_only(tmp_path):
    cwd = str(tmp_path / "billing-app")
    conv, supplied = read_transcript(str(_write_transcript(tmp_path / "t.jsonl", cwd)), cwd)
    text = json.dumps(conv)
    assert [m["role"] for m in conv] == ["user", "assistant"]
    assert "BILL-311" in text and "finance requires commercial rounding" in text and "Fixed" in text
    for leaked in ("someone@example.com", "context injected", "private reasoning", "SECRET FILE CONTENTS",
                   "old goal", "meta prompt"):
        assert leaked not in text
    assert supplied == {"key_facts": ["Staging deploy is on Thursday"], "files": ["billing.py"]}


def _run(command, event, monkeypatch, data_dir):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(data_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hooks.main([command]) == 0
    return out.getvalue()


def test_save_then_session_start_injects_this_projects_brief(tmp_path, monkeypatch):
    cwd = tmp_path / "billing-app"
    cwd.mkdir()
    transcript = _write_transcript(tmp_path / "t.jsonl", str(cwd))
    event = {"session_id": "s-1", "transcript_path": str(transcript), "cwd": str(cwd)}
    assert _run("save", event, monkeypatch, tmp_path / "mem") == ""

    out = json.loads(_run("session-start", {"cwd": str(cwd), "source": "startup"}, monkeypatch, tmp_path / "mem"))
    brief = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    for value in ("BILL-311", "ROUND_HALF_UP", "finance requires commercial rounding", "12,34 €", "Anika",
                  "Thursday", "billing.py"):
        assert value in brief
    assert "sk-test-FAKE" not in brief

    other = tmp_path / "other-app"
    other.mkdir()
    assert _run("session-start", {"cwd": str(other), "source": "startup"}, monkeypatch, tmp_path / "mem") == ""


def test_repeated_saves_update_one_session(tmp_path, monkeypatch):
    cwd = tmp_path / "app"
    cwd.mkdir()
    transcript = _write_transcript(tmp_path / "t.jsonl", str(cwd))
    event = {"session_id": "s-1", "transcript_path": str(transcript), "cwd": str(cwd)}
    _run("save", event, monkeypatch, tmp_path / "mem")
    _run("save", event, monkeypatch, tmp_path / "mem")  # Stop fires after every turn
    _write_transcript(transcript, str(cwd), [_line("user", "Also the owner of this module is Ravi.")])
    _run("save", event, monkeypatch, tmp_path / "mem")

    conn = sqlite3.connect(tmp_path / "mem" / "mcp_memories.db")
    sessions = conn.execute("SELECT session_id, handoff_json FROM sessions").fetchall()
    summaries = conn.execute("SELECT COUNT(*) FROM memory_entries WHERE category = 'session_summary'").fetchone()[0]
    texts = [r[0] for r in conn.execute("SELECT text FROM memory_entries")]
    assert len(sessions) == 1 and "Ravi" in sessions[0][1]
    assert summaries == 1
    assert len(texts) == len(set(texts))


def test_hooks_never_fail_the_session(tmp_path, monkeypatch):
    bad = {"session_id": "x", "transcript_path": str(tmp_path / "missing.jsonl"), "cwd": str(tmp_path)}
    assert _run("save", bad, monkeypatch, tmp_path / "mem") == ""
    monkeypatch.setattr(hooks, "save", lambda event: 1 / 0)
    monkeypatch.setitem(hooks.COMMANDS, "save", hooks.save)
    assert _run("save", bad, monkeypatch, tmp_path / "mem") == ""
    assert "ZeroDivisionError" in (tmp_path / "mem" / "hooks.log").read_text()


def test_hooks_can_be_switched_off(tmp_path, monkeypatch):
    cwd = tmp_path / "app"
    cwd.mkdir()
    event = {"session_id": "s", "transcript_path": str(_write_transcript(tmp_path / "t.jsonl", str(cwd))),
             "cwd": str(cwd)}
    monkeypatch.setenv("COGNICORE_AUTO_CAPTURE", "0")
    _run("save", event, monkeypatch, tmp_path / "mem")
    assert not (tmp_path / "mem" / "mcp_memories.db").exists()


@pytest.mark.parametrize("statement", ["The staging deploy is Thursday and the reviewer is Anika."])
def test_assistant_restatements_are_merged(statement):
    from cognicore.memory.handoff import build_handoff

    conv = [{"role": "user", "content": "FYI the staging deploy is on Thursday and the reviewer is Anika."},
            {"role": "assistant", "content": statement}]
    h = build_handoff(conv, "s", "n", 0.0)
    assert sum("Anika" in f for f in [h["goal"], *h["key_facts"]]) == 1

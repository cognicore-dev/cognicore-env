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
    monkeypatch.setattr(hooks, "save", lambda event, **kw: 1 / 0)
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


# ---------------------------------------------------------------------------
# Codex: session-log parsing and hooks
# ---------------------------------------------------------------------------

def _codex_rollout(path, cwd):
    """A Codex rollout in the format Codex 0.164 writes (shapes copied from a real log)."""
    def item(payload):
        return json.dumps({"timestamp": "2026-11-01T15:42:35Z", "type": "response_item", "payload": payload})
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "c-1", "cwd": cwd, "base_instructions": {"text": "SYSTEM"}}}),
        item({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are Codex. SECRET SYSTEM PROMPT"}]}),
        item({"type": "message", "role": "user", "content": [{"type": "input_text", "text":
             f"<environment_context>\n  <cwd>{cwd}</cwd>\n</environment_context>"}]}),
        item({"type": "message", "role": "user", "content": [{"type": "input_text", "text":
             "Ticket CALC-9: add() subtracts. We decided to keep the signature because callers depend on it. "
             "The reviewer is Lena. Token sk-test-FAKE0000000000000000000000."}]}),
        item({"type": "custom_tool_call", "name": "exec", "input":
             """await tools.exec_command({cmd:"Set-Content -Path calc.py -Value @('def add(a, b):')"})"""}),
        item({"type": "custom_tool_call_output", "output": "FILE CONTENTS THAT MUST NOT BE STORED"}),
        item({"type": "function_call", "name": "apply_patch",
              "arguments": json.dumps({"input": "*** Begin Patch\n*** Update File: tests/test_calc.py\n*** End Patch"})}),
        item({"type": "message", "role": "assistant", "content": [{"type": "output_text",
             "text": "Changed add() in calc.py to return a + b, keeping the signature."}]}),
        json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_read_codex_rollout(tmp_path):
    cwd = str(tmp_path / "calc-app")
    conv, supplied = read_transcript(str(_codex_rollout(tmp_path / "rollout.jsonl", cwd)), cwd)
    text = json.dumps(conv)
    assert [m["role"] for m in conv] == ["user", "assistant"]
    assert "CALC-9" in text and "callers depend on it" in text and "Lena" in text and "a + b" in text
    for leaked in ("SECRET SYSTEM PROMPT", "environment_context", "FILE CONTENTS"):
        assert leaked not in text
    assert supplied["files"] == ["calc.py", "tests/test_calc.py"]


def test_codex_save_then_claude_session_start(tmp_path, monkeypatch):
    """Work saved by Codex's hooks is resumed by Claude Code's, in the same project."""
    cwd = tmp_path / "calc-app"
    cwd.mkdir()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", str(cwd))
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "c-1", "transcript_path": str(rollout), "cwd": str(cwd), "hook_event_name": "Stop"})))
    assert hooks.main(["save", "--agent", "codex"]) == 0
    out = json.loads(_run("session-start", {"cwd": str(cwd), "source": "startup"}, monkeypatch, tmp_path / "mem"))
    brief = out["hookSpecificOutput"]["additionalContext"]
    assert "CALC-9" in brief and "Lena" in brief and "by codex" in brief and "sk-test-FAKE" not in brief
    assert _run("session-start", {"cwd": str(cwd), "source": "resume"}, monkeypatch, tmp_path / "mem") == ""


def test_setup_codex_hooks_merges_and_removes(tmp_path):
    from cognicore.agent_setup import setup_codex, setup_codex_hooks

    target = tmp_path / "hooks.json"
    mine = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}]}}
    target.write_text(json.dumps(mine), encoding="utf-8")

    assert "/hooks" in setup_codex_hooks(str(tmp_path), python="C:/py/python.exe")
    setup_codex_hooks(str(tmp_path), python="C:/py/python.exe")  # re-run replaces, no duplicates
    doc = json.loads(target.read_text(encoding="utf-8"))
    commands = [h["command"] for groups in doc["hooks"].values() for g in groups for h in g["hooks"]]
    assert "notify-send done" in commands
    assert sorted(c for c in commands if "cognicore.hooks" in c) == sorted([
        '"C:/py/python.exe" -m cognicore.hooks session-start --agent codex',
        '"C:/py/python.exe" -m cognicore.hooks save --agent codex',
        '"C:/py/python.exe" -m cognicore.hooks save --agent codex',
    ])
    assert "SessionEnd" not in doc["hooks"]  # Codex gives SessionEnd 1-3 s: too short to save

    assert "Removed" in setup_codex(str(tmp_path), remove=True)
    assert json.loads(target.read_text(encoding="utf-8")) == mine
    assert "nothing to remove" in setup_codex_hooks(str(tmp_path), remove=True)


def test_codex_agents_md_injection_is_not_saved(tmp_path):
    rollout = tmp_path / "r.jsonl"
    msg = ("# AGENTS.md instructions\n\n<INSTRUCTIONS>\n<!-- cognicore:begin -->\r\n## CogniCore memory\r\nrule text"
           "\r\n<!-- cognicore:end -->\n</INSTRUCTIONS>")
    rollout.write_text("\n".join([
        json.dumps({"type": "session_meta", "payload": {"cwd": str(tmp_path)}}),
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": msg}]}}),
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "Fix the INC-7781 retries."}]}}),
    ]), encoding="utf-8")
    conv, _ = read_transcript(str(rollout), str(tmp_path))
    assert conv == [{"role": "user", "content": "Fix the INC-7781 retries."}]


def test_continuation_session_keeps_the_project_goal(tmp_path, monkeypatch):
    """Claude saves the task; a later Codex session that only says "continue" keeps that goal."""
    cwd = tmp_path / "billing-app"
    cwd.mkdir()
    first = _write_transcript(tmp_path / "t1.jsonl", str(cwd))
    _run("save", {"session_id": "s-1", "transcript_path": str(first), "cwd": str(cwd)}, monkeypatch, tmp_path / "mem")

    rollout = tmp_path / "r.jsonl"
    rollout.write_text("\n".join(json.dumps(x) for x in [
        {"type": "session_meta", "payload": {"cwd": str(cwd)}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text",
         "text": "Continue where the previous session left off. Don't ask me questions."}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text",
         "text": "format_eur now returns '12,34 €' and its test passes."}]}},
    ]), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "c-2", "transcript_path": str(rollout), "cwd": str(cwd)})))
    hooks.main(["save", "--agent", "codex"])

    conn = sqlite3.connect(tmp_path / "mem" / "mcp_memories.db")
    goals = dict(conn.execute("SELECT agent, json_extract(handoff_json, '$.goal') FROM sessions").fetchall())
    assert "BILL-311" in goals["claude-code"]
    assert goals["codex"] == goals["claude-code"]


def test_stated_goal():
    from cognicore.memory.handoff import stated_goal

    assert stated_goal([{"role": "user", "content": "Continue where we left off."}]) == ""
    assert "migrating" in stated_goal([{"role": "user", "content": "We are migrating users to Postgres 16."}])

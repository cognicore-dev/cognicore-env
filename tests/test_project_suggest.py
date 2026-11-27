"""Project context, part 2: provenance and ids, Key values, and suggest-then-accept."""
import io
import json

import pytest

from cognicore import hooks
from cognicore import project_context as pc
from cognicore import project_suggest as ps


@pytest.fixture
def repo(tmp_path, monkeypatch):
    project = tmp_path / "billing-app"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")
    monkeypatch.setenv("COGNICORE_SYNC", "0")
    monkeypatch.delenv("COGNICORE_SUGGEST", raising=False)
    monkeypatch.setattr(pc, "person", lambda: "Anika")
    monkeypatch.chdir(project)
    return project


def _text(repo):
    return (repo / ".cognicore" / "context.md").read_text(encoding="utf-8")


# --- ids, provenance, Key values -------------------------------------------------------

def test_items_record_date_source_and_a_stable_id(repo):
    pc.add("decision", "Use Decimal for money", why="finance requires commercial rounding")
    pc.add("rule", "Never put keys in git", provenance=pc.source(by="claude-code", session="sess_1"))
    pc.add("value", "Staging deploy is on Thursday")
    text = _text(repo)
    assert "## Key values" in text
    assert "- Use Decimal for money (why: finance requires commercial rounding) [id: D1; date: " in text
    assert "; by: Anika]" in text
    assert "- Never put keys in git [id: R1; date: " in text and "by: claude-code; session: sess_1]" in text
    ctx = pc.load()
    assert ctx["facts"][0]["text"] == "Staging deploy is on Thursday" and ctx["facts"][0]["id"] == "V1"


def test_ids_survive_removal_and_hand_written_items_get_the_next_one(repo):
    for r in ("A rule", "B rule", "C rule"):
        pc.add("rule", r)
    assert pc.remove("R2") == "B rule"
    target = repo / ".cognicore" / "context.md"
    target.write_text(_text(repo) + "- D rule written by hand\n", encoding="utf-8")
    assert [(r["id"], r["text"]) for r in pc.load()["rules"]] == [("R1", "A rule"), ("R3", "C rule"),
                                                                   ("R4", "D rule written by hand")]
    assert pc.remove("r9") is None


def test_old_facts_heading_and_brackets_in_text_still_parse(repo):
    target = repo / ".cognicore" / "context.md"
    target.parent.mkdir()
    target.write_text("## Facts\n- Ticket [WIP] is BILL-311\n- Port is 8080 [id: V7; by: Ravi]\n", encoding="utf-8")
    facts = pc.load()["facts"]
    assert facts[0]["text"] == "Ticket [WIP] is BILL-311" and facts[1] == {"text": "Port is 8080", "id": "V7",
                                                                          "by": "Ravi"}
    pc.add("value", "Region is eu-west-1")
    assert "## Key values" in _text(repo) and "[WIP]" in _text(repo)


def test_text_that_looks_like_provenance_is_not_misread(repo):
    pc.add("value", "Use the tag [by: release-bot] on images")
    pc.add("value", "Images are tagged [by: release-bot]")
    assert [f["text"] for f in pc.load()["facts"]] == ["Use the tag [by: release-bot] on images",
                                                       "Images are tagged (by: release-bot)"]


def test_cli_remove_by_id_and_list_shows_ids(repo, capsys):
    pc.add("decision", "Use Sarvam", why="Indian-hosted")
    assert pc.main(["remove", "D1"]) == 0 and "Removed: Use Sarvam" in capsys.readouterr().out
    pc.add("rule", "Run tests first")
    pc.main(["list"])
    assert "  R1. Run tests first  (" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        pc.main(["remove", "nonsense"])


# --- suggestions ------------------------------------------------------------------------

def test_propose_accept_writes_with_provenance(repo, capsys):
    res = ps.propose("decision", "Use Postgres", why="the team knows it", by="cursor", session="sess_9")
    assert res["n"] == 1
    assert pc.load()["decisions"] == []  # proposed, not written
    assert pc.main(["accept", "1"]) == 0
    out = capsys.readouterr().out
    assert "Accepted: Decision: Use Postgres (why: the team knows it)" in out
    d = pc.load()["decisions"][0]
    assert (d["what"], d["why"], d["by"], d["session"]) == ("Use Postgres", "the team knows it", "cursor", "sess_9")
    assert ps.pending() == []
    assert ps.propose("decision", "Use Postgres", why="x")["skipped"] == "already in the project context"


def test_dismissed_items_are_not_suggested_again(repo):
    n = ps.propose("rule", "Never deploy on Fridays")["n"]
    assert ps.dismiss([n])[0] == ["Rule: Never deploy on Fridays"]
    assert ps.propose("rule", "Never deploy on Fridays")["skipped"] == "suggested before"


def test_accept_unknown_numbers_and_goal(repo, capsys):
    ps.propose("goal", "Ship the EU billing release")
    assert pc.main(["accept", "1", "7"]) == 0
    assert "No suggestion numbered 7" in capsys.readouterr().out
    assert pc.load()["goal"] == "Ship the EU billing release"
    assert pc.main(["accept", "5"]) == 1


def test_propose_rejects_bad_input_and_redacts(repo):
    assert "unknown kind" in ps.propose("wish", "x y z")["skipped"]
    assert ps.propose("rule", "x")["skipped"]
    res = ps.propose("value", "Staging key is sk-test-FAKE0000000000000000000000")
    assert res["redacted"] and "sk-test-FAKE" not in json.dumps(ps.pending())


def test_malformed_store_is_ignored(repo, tmp_path):
    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "mem" / ps.STORE).write_text("{not json", encoding="utf-8")
    assert ps.pending() == []
    (tmp_path / "mem" / ps.STORE).write_text(json.dumps({ps._key(): {"pending": [{"n": "x"}, 5], "seen": 3}}),
                                            encoding="utf-8")
    assert ps.pending() == [] and ps.propose("rule", "Always run the linter")["n"] == 1


def test_rule_sentences_keep_rules_and_skip_chatter():
    conv = [{"role": "user", "content": "Always run the billing tests before pushing. Don't worry about the docs. "
                                        "Never put keys in git! Don't merge anything?\nnever mind\n"
                                        "We never deploy on Fridays."},
            {"role": "assistant", "content": "Never use floats for money."}]
    assert ps.rule_sentences(conv) == ["Always run the billing tests before pushing.", "Never put keys in git.",
                                       "We never deploy on Fridays."]


def test_candidates_need_a_reason_and_skip_what_is_known():
    handoff = {"decisions": [{"what": "We decided to use Decimal.", "why": "finance requires rounding."},
                             {"what": "Use floats everywhere", "why": ""},
                             {"what": "The file has CRLF line endings", "why": "I'll switch to the Edit tool"},
                             {"what": "Use the old API", "why": "x", "status": "rejected"}]}
    ctx = pc.empty()
    ctx["rules"] = [{"text": "Never put keys in git"}]
    conv = [{"role": "user", "content": "Never put keys in git. The goal is to ship EU billing by Friday."}]
    got = ps.candidates(handoff, conv, ctx)
    assert got[0] == ("decision", "Use Decimal", "finance requires rounding")
    assert all(k != "rule" for k, _, _ in got)
    for noise in ("Use floats everywhere", "old API", "CRLF"):
        assert noise not in json.dumps(got)


def _run(command, event, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hooks.main([command]) == 0
    return out.getvalue()


def _transcript(path, text):
    path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n",
                    encoding="utf-8")
    return path


def test_stop_hook_suggests_at_most_three_once(repo, tmp_path, monkeypatch):
    t = _transcript(tmp_path / "t.jsonl", "We decided to use Decimal because finance requires commercial rounding. "
                    "Always run the billing tests before pushing. Never put keys in git. "
                    "Never deploy on Fridays. Don't use floats for money.")
    event = {"session_id": "s1", "transcript_path": str(t), "cwd": str(repo), "hook_event_name": "Stop"}
    msg = json.loads(_run("save", event, monkeypatch))["systemMessage"]
    assert msg.startswith("CogniCore: 3 suggested for project context: 1. ")
    assert msg.endswith("Accept with `cognicore project accept 1 2 3`.")
    _transcript(t, "We decided to use Decimal because finance requires commercial rounding. "
                   "Always run the billing tests before pushing. Never put keys in git. "
                   "Never deploy on Fridays. Don't use floats for money. Thanks.")
    again = _run("save", event, monkeypatch)
    assert "1. " not in again  # shown once; the remaining ones are new
    assert pc.load()["rules"] == []  # nothing is written until accepted


def test_stop_hook_suggestions_can_be_switched_off(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_SUGGEST", "0")
    t = _transcript(tmp_path / "t.jsonl", "Never put keys in git. Always run the tests first.")
    assert _run("save", {"session_id": "s2", "transcript_path": str(t), "cwd": str(repo)}, monkeypatch) == ""


def test_a_broken_suggestion_never_breaks_the_save(repo, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad")

    monkeypatch.setattr(ps, "from_session", boom)
    t = _transcript(tmp_path / "t.jsonl", "Never put keys in git. We use Postgres because the team knows it.")
    assert _run("save", {"session_id": "s3", "transcript_path": str(t), "cwd": str(repo)}, monkeypatch) == ""
    assert "project suggestions skipped: RuntimeError" in (tmp_path / "mem" / "hooks.log").read_text(encoding="utf-8")


def test_mcp_tool_proposes_but_cannot_accept(repo, monkeypatch):
    pytest.importorskip("mcp")
    from cognicore.mcp.server import create_mcp_server as create_server

    monkeypatch.setenv("COGNICORE_TOOLSETS", "search,project")
    server = create_server()
    tools = server._tool_manager._tools
    assert set(tools) == {"cognicore_recall", "cognicore_project_propose"}  # file card tools are opt-in
    out = json.loads(tools["cognicore_project_propose"].fn(kind="rule", text="Never commit .env files", cwd=str(repo)))
    assert out == {"queued": True, "number": 1, "item": "Rule: Never commit .env files",
                   "accept_with": "cognicore project accept 1"}
    assert pc.load(str(repo))["rules"] == []
    assert not any("accept" in name for name in tools)


def test_no_network(repo, monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    ps.propose("rule", "Never put keys in git")
    assert ps.accept([1])[0]

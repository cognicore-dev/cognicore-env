"""Curated project context: the .cognicore/context.md file, its commands, and the brief reading it first."""
import io
import json
import subprocess

import pytest

from cognicore import hooks
from cognicore import project_context as pc
from cognicore.memory.context_preservation import TokenBudget


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A project folder that is its own repository, with memory in a separate data folder."""
    project = tmp_path / "billing-app"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")
    monkeypatch.setenv("COGNICORE_SYNC", "0")
    monkeypatch.delenv("COGNICORE_PROJECT_TOKENS", raising=False)
    monkeypatch.chdir(project)
    return project


def _core(items):
    """Items without their provenance (id, date, by, session)."""
    return [{k: v for k, v in i.items() if k in ("what", "why", "status", "text")} for i in items]


def _cli(*argv, capsys):
    code = pc.main(list(argv))
    return code, capsys.readouterr().out


def test_the_file_lives_at_the_repository_root(repo):
    sub = repo / "src" / "billing"
    sub.mkdir(parents=True)
    assert pc.path(str(sub)) == repo / ".cognicore" / "context.md"


def test_a_repository_at_the_home_folder_does_not_count(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "Documents" / "app"
    project.mkdir(parents=True)
    (home / ".git").mkdir()
    monkeypatch.setattr(pc.Path, "home", classmethod(lambda cls: home))
    assert pc.root(str(project)) == project.resolve()
    assert pc.root(str(home)) == home.resolve()  # the home folder itself still can be a project


def test_commands_write_a_readable_file_and_list_it(repo, capsys):
    _cli("set", "goal", "Ship", "Sarvam", "summaries", capsys=capsys)
    _cli("set", "status", "Summaries live; recall quality next", capsys=capsys)
    _cli("add", "decision", "Rule-based stays default", "--why", "zero cost, local", capsys=capsys)
    _cli("add", "decision", "Hosted MCP save per turn", "--rejected", "--why", "no session key", capsys=capsys)
    _cli("add", "rule", "Never put keys in git", capsys=capsys)
    _cli("add", "fact", "Hosted server runs commit 87632787", capsys=capsys)

    text = (repo / ".cognicore" / "context.md").read_text(encoding="utf-8")
    assert "## Goal\n\nShip Sarvam summaries\n" in text
    assert "- Rule-based stays default (why: zero cost, local)" in text
    assert "- Rejected: Hosted MCP save per turn (why: no session key)" in text
    assert "## Rules\n\n- Never put keys in git" in text

    code, out = _cli("list", capsys=capsys)
    assert code == 0
    assert "Goal: Ship Sarvam summaries" in out and "  D2. Rejected: Hosted MCP save per turn" in out


def test_hand_edits_survive_a_round_trip(repo):
    hand = """# Project context

## Goal
Fix BILL-311
rounding in format_eur

## Decisions
1. Use ROUND_HALF_UP (why: finance requires commercial rounding)
* rejected: banker's rounding
  because nobody asked for it

## Owners
- Billing: Anika

## rules
- Run the billing tests before pushing
"""
    target = repo / ".cognicore" / "context.md"
    target.parent.mkdir()
    target.write_text(hand, encoding="utf-8")
    ctx = pc.load()
    assert ctx["goal"] == "Fix BILL-311 rounding in format_eur"
    assert _core(ctx["decisions"]) == [
        {"what": "Use ROUND_HALF_UP", "why": "finance requires commercial rounding", "status": "made"},
        {"what": "banker's rounding because nobody asked for it", "why": "", "status": "rejected"}]
    assert _core(ctx["rules"]) == [{"text": "Run the billing tests before pushing"}]
    pc.add("fact", "Totals are in EUR")
    again = target.read_text(encoding="utf-8")
    assert "## Owners\n- Billing: Anika" in again  # an unknown section is kept as written
    assert pc.parse(again)["decisions"] == ctx["decisions"]


def test_repeats_are_not_added_and_a_decision_can_be_updated(repo):
    pc.add("rule", "Never put keys in git")
    _, _, added = pc.add("rule", "never put  keys in git")
    assert not added and len(pc.load()["rules"]) == 1
    pc.add("decision", "Use Sarvam", why="Indian-hosted")
    _, _, changed = pc.add("decision", "Use Sarvam", why="Indian-hosted", rejected=True)
    assert changed and _core(pc.load()["decisions"]) == [{"what": "Use Sarvam", "why": "Indian-hosted",
                                                     "status": "rejected"}]


def test_secrets_are_redacted_before_writing(repo, capsys):
    code, out = _cli("add", "fact", "Staging key is sk-test-FAKE0000000000000000000000", capsys=capsys)
    assert code == 0 and "redacted" in out
    assert "sk-test-FAKE" not in (repo / ".cognicore" / "context.md").read_text(encoding="utf-8")


def test_remove_by_number_and_goal(repo, capsys):
    pc.set_text("goal", "Old goal")
    for rule in ("A rule", "B rule", "C rule"):
        pc.add("rule", rule)
    code, out = _cli("remove", "rule", "2", capsys=capsys)
    assert code == 0 and "Removed: B rule" in out
    assert [r["text"] for r in pc.load()["rules"]] == ["A rule", "C rule"]
    assert _cli("remove", "rule", "9", capsys=capsys)[0] == 1
    assert pc.remove("goal") == "Old goal" and pc.load()["goal"] == ""


def test_history_is_the_git_log_of_the_file(tmp_path, monkeypatch, capsys):
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.chdir(project)

    def git(*args):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", *args],
                       cwd=project, check=True, capture_output=True)

    git("init", "-q")
    assert "doesn't exist yet" in pc.history()
    pc.set_text("goal", "First goal")
    assert "not committed yet" in pc.history()
    git("add", ".cognicore/context.md")
    git("commit", "-q", "-m", "project: set the goal")
    code, out = _cli("history", "--diff", capsys=capsys)
    assert code == 0 and "project: set the goal" in out and "+First goal" in out


# --- the brief ------------------------------------------------------------------------

def _session_start(cwd, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(cwd), "source": "startup"})))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hooks.main(["session-start"]) == 0
    raw = out.getvalue()
    return json.loads(raw)["hookSpecificOutput"]["additionalContext"] if raw else ""


def _save_session(cwd):
    from cognicore.memory.context_preservation import save_session

    save_session(hooks._backend(), session_name="billing-app", agent="claude-code", project=str(cwd),
                 session_key="s-1", supplied_handoff={
                     "goal": "Right, so the totals thing", "current_state": "Rounding merged in #42; docs pending.",
                     "decisions": [{"what": "Use ROUND_HALF_UP", "why": "finance requires it"},
                                   {"what": "Keep format_eur in billing/tax.py", "why": "one place for money"}],
                     "open_tasks": ["Update docs/billing.md for BILL-311"]})


def test_the_curated_goal_replaces_the_guessed_one_and_comes_first(repo, monkeypatch):
    _save_session(repo)
    pc.set_text("goal", "Fix BILL-311 rounding in format_eur")
    pc.add("rule", "Never put keys in git")
    pc.add("decision", "Use ROUND_HALF_UP", why="finance requires commercial rounding")

    brief = _session_start(repo, monkeypatch)
    lines = brief.splitlines()
    assert lines[0] == "=== COGNICORE CONTEXT RESUME ==="
    assert lines[1].startswith("Project context (.cognicore/context.md")
    assert lines.index("Goal: Fix BILL-311 rounding in format_eur") < next(
        i for i, line in enumerate(lines) if line.startswith("Session:"))
    assert "Right, so the totals thing" not in brief  # the guessed goal is gone
    assert "- Never put keys in git" in brief
    assert brief.count("Use ROUND_HALF_UP") == 1  # stated once, with the curated reason
    assert "finance requires commercial rounding" in brief and "Keep format_eur in billing/tax.py" in brief
    assert "Update docs/billing.md for BILL-311" in brief  # session progress still follows
    assert TokenBudget.estimate_tokens(brief) <= 700 + 20


def test_without_a_curated_goal_the_session_goal_stays(repo, monkeypatch):
    _save_session(repo)
    pc.add("rule", "Never put keys in git")
    brief = _session_start(repo, monkeypatch)
    assert "Right, so the totals thing" in brief and "- Never put keys in git" in brief


def test_no_file_means_the_brief_is_unchanged(repo, monkeypatch):
    _save_session(repo)
    brief = _session_start(repo, monkeypatch)
    assert "Project context" not in brief and "Right, so the totals thing" in brief


def test_a_project_with_only_curated_context_still_gets_a_brief(repo, monkeypatch):
    assert _session_start(repo, monkeypatch) == ""
    pc.set_text("goal", "Fix BILL-311")
    brief = _session_start(repo, monkeypatch)
    assert brief.startswith("=== COGNICORE CONTEXT RESUME ===\nProject context")
    assert "Goal: Fix BILL-311" in brief and "=== END CONTEXT" in brief


def test_the_project_share_of_the_brief_is_capped(repo, monkeypatch):
    monkeypatch.setenv("COGNICORE_PROJECT_TOKENS", "120")
    pc.set_text("goal", "Fix BILL-311")
    for n in range(40):
        pc.add("fact", f"Fact number {n} about the billing rollout and its many details")
    lines = pc.brief_lines(pc.load())
    assert TokenBudget.estimate_tokens("\n".join(lines)) <= 120
    assert lines[-1].startswith("(+") and "cognicore project list" in lines[-1]


def test_the_brief_is_still_stripped_from_saved_transcripts(repo):
    from cognicore.memory.transcript import _INJECTED

    pc.set_text("goal", "Fix BILL-311")
    brief = pc.standalone_brief(pc.load())
    assert not any(p.sub("", brief).strip() for p in _INJECTED if p.search(brief)) and any(
        p.search(brief) for p in _INJECTED)


def test_the_cognicore_command_routes_to_project(repo, monkeypatch, capsys):
    from cognicore import cli

    monkeypatch.setattr("sys.argv", ["cognicore", "project", "add", "decision", "X", "--why", "Y"])
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code == 0
    assert _core(pc.load()["decisions"]) == [{"what": "X", "why": "Y", "status": "made"}]

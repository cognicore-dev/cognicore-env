"""The connector checks a user runs from claude.ai: stats, latest work, sessions, dates,
agents, staleness, noise, exact values, project labels and project context.

Each test seeds a hosted account the way sync does (``import_handoff``) and reads it
back through the hosted tools, so what is checked is what claude.ai sees.
"""
import json
import time
from datetime import date
from types import SimpleNamespace

import jwt
import pytest

pytest.importorskip("fastapi")

from cognicore import hooks  # noqa: E402
from cognicore.connectors import remote  # noqa: E402
from cognicore.memory import brief_filters as bf  # noqa: E402
from cognicore.memory.context_preservation import import_handoff, resume_session, save_session  # noqa: E402
from cognicore.memory.handoff import SCHEMA_VERSION, build_handoff, display_name, session_dates  # noqa: E402

ALICE = "github:4242"


def _ctx(user_id=ALICE):
    token = jwt.encode({"sub": user_id, "typ": "access", "login": user_id, "exp": int(time.time()) + 600},
                       remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM)
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"}, query_params={})
    return SimpleNamespace(request_context=SimpleNamespace(request=request, meta=None))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")
    monkeypatch.delenv("COGNICORE_DB_PATH", raising=False)
    monkeypatch.delenv("COGNICORE_SYNC", raising=False)
    monkeypatch.delenv("COGNICORE_AI_COMPRESS", raising=False)


def _doc(session_id, created, goal="", state="", completed=(), decisions=(), tasks=(), facts=(), files=(),
         agent="claude-code", name="cognicore-my-openenv", updated="", **extra):
    prov = {"session_id": session_id, "session_name": name, "agent": agent, "client": f"{agent}-hooks",
            "created_at": created, "message_count": 3, "migrated": False, "project": ""}
    if updated:
        prov["updated_at"] = updated
    return {"schema": SCHEMA_VERSION, "provenance": prov, "goal": goal, "current_state": state,
            "completed": list(completed), "decisions": [{"what": w, "why": y, "status": "made"} for w, y in decisions],
            "open_tasks": [{"task": t, "status": "open"} for t in tasks], "key_facts": list(facts),
            "exact_values": [], "files": list(files), "code": [], "environment": {}, "redactions": 0, **extra}


def _ts(day: str) -> float:
    return time.mktime(time.strptime(day, "%Y-%m-%d")) + 12 * 3600


@pytest.fixture
def account():
    backend = remote.get_backend(_ctx())
    import_handoff(backend, _doc("sess_20261104_074224_50e0ed3e", "2026-11-04T07:42:24Z",
                                 goal="Recall test: prove a note saved in Claude Code can be recalled in claude.ai",
                                 facts=["The recall test code word is BLUE-HERON-4417"], name="safetymind"),
                   "claude-code:a", _ts("2026-11-04"))
    import_handoff(backend, _doc("sess_20261115_051727_8db64038", "2026-11-15T05:17:27Z",
                                 goal="Tune the Sarvam summaries on the hosted server",
                                 completed=["The Sarvam tuning is built and pushed as `feat/sarvam-tuning`."],
                                 agent="codex"),
                   "codex:b", _ts("2026-11-15") - 3600)
    import_handoff(backend, _doc(
        "sess_20261115_153609_f32d222e", "2026-11-15T15:36:09Z", updated="2026-11-17T09:00:00Z",
        goal="Recall test: prove a note saved in Claude Code can be recalled in claude.ai",
        state="Brief puts project context first. Nothing is merged. Part 5 mostly exists.",
        completed=["PR #227 (project context) is already merged into `cognicore-dev/main`.",
                   "In both parts the full suite gives 1470 passed, 7 skipped, and only the 3 known failures."],
        decisions=[("The file has CRLF line endings", "I'll switch to the Edit tool."),
                   ('Surface it: in the brief ("Conflict: Cursor decided X on Nov 14', ""),
                   ("PART 2 — decision history with proof (branch feat/decision-provenance, stacked on part 1)", ""),
                   ("Every decision records: source (session id, agent, date)", ""),
                   ("Store the project context in one readable file, .cognicore/context.md, because it is reviewable",
                    "")],
        tasks=["Full suite must pass (note the 3 known pre-existing failures).",
               "Publish 0.13.0 to PyPI"],
        facts=['Which stands?") and via `cognicore project conflicts`', "Before you merge", "It's on the local server only.",
               "Part 0 is the stable project key on `fix/project-key`."],
        files=["cognicore-my-openenv/cognicore/repo.py", "cognicore-my-openenv/cognicore/hooks.py",
               "cognicore-my-openenv/tests/test_project_key.py", "cognicore-my-openenv/cognicore/project_context.py"],
        name="safetymind",
        project_context={"goal": "Give every agent one shared, curated project memory", "status": "Parts 0-1 pushed",
                         "decisions": [], "rules": [{"text": "Never put keys in git", "date": "2026-11-15",
                                                     "by": "Kaushal T"}], "values": []}),
        "claude-code:c", _ts("2026-11-17"))
    return backend


# 1. CONNECTION ---------------------------------------------------------------------------

def test_stats_count_session_facts_too(account):
    remote.cognicore_remember("Prefers short answers", _ctx(), category="profile")
    out = remote.cognicore_stats(_ctx())
    with account._get_conn() as conn:
        stored = conn.execute("SELECT COUNT(*) FROM memory_entries WHERE state NOT IN ('archived','deleted')"
                              ).fetchone()[0]
    assert f"Total memories: {stored} (1 remembered notes" in out
    assert "from 3 saved sessions" in out
    assert "completed:" in out  # session categories are listed, not only the notes'


# 2. LATEST WORK ------------------------------------------------------------------------

def test_latest_brief_dates_and_goal(account):
    out = remote.cognicore_resume_session(_ctx())
    head = next(line for line in out.splitlines() if line.startswith("Session:"))
    assert "2026-11-15 (updated 2026-11-17)" in head
    assert "by claude-code" in head
    # The carried Nov 4 goal is not presented as this session's goal; the project goal is.
    assert "Goal: Recall test" not in out
    assert "Give every agent one shared, curated project memory" in out


def test_resave_keeps_start_date(tmp_path):
    backend = hooks._backend()
    conv = [{"role": "user", "content": "Our goal is to fix the brief dates."}]
    first = json.loads(save_session(backend, conv, session_key="k", session_name="p"))
    with backend._get_conn() as conn:
        conn.execute("UPDATE sessions SET handoff_json = REPLACE(handoff_json, ?, ?)",
                     (json.loads(conn.execute("SELECT handoff_json FROM sessions").fetchone()[0])
                      ["provenance"]["created_at"], "2026-11-15T10:00:00Z"))
    conv.append({"role": "assistant", "content": "Committed the date fix as abc1234."})
    again = json.loads(save_session(backend, conv, session_key="k", session_name="p"))
    assert again["session_id"] == first["session_id"]
    with backend._get_conn() as conn:
        prov = json.loads(conn.execute("SELECT handoff_json FROM sessions").fetchone()[0])["provenance"]
    assert prov["created_at"].startswith("2026-11-15")
    assert prov["updated_at"] > prov["created_at"]


# 3. MULTIPLE SESSIONS -----------------------------------------------------------------

def test_sessions_list_ids_dates_and_agents(account):
    out = remote.cognicore_resume_session(_ctx(), last_n_sessions=5, max_tokens=4000)
    assert "sess_20261115_051727_8db64038, 2026-11-15, by codex" in out
    assert "sess_20261104_074224_50e0ed3e, 2026-11-04, by claude-code" in out


def test_duplicate_session_listed_once(account):
    dup = _doc("sess_20261103_151101_df90683f", "2026-11-15T01:00:00Z",
               goal="Recall test: prove a note saved in Claude Code can be recalled in claude.ai",
               facts=["The recall test code word is BLUE-HERON-4417"], name="safetymind")
    import_handoff(account, dup, "claude-code:old", _ts("2026-11-15") - 7200)
    out = remote.cognicore_resume_session(_ctx(), last_n_sessions=5, max_tokens=4000)
    assert out.count("BLUE-HERON-4417") == 1


def test_synced_session_keeps_its_id(account):
    out = remote.cognicore_resume_session(_ctx(), session_id="sess_20261115_153609_f32d222e", section="completed")
    assert "PR #227" in out


def test_session_synced_under_another_id_is_renamed(account):
    doc = _doc("sess_20261116_101010_0961ed90", "2026-11-16T10:10:10Z", goal="Rename me")
    with account._get_conn() as conn:
        conn.execute("UPDATE sessions SET session_id = 'sess_20261116_999999_aaaaaaaa' "
                     "WHERE external_id = 'claude-code:a'")
    import_handoff(account, {**doc, "goal": "Recall test v2"}, "claude-code:a", _ts("2026-11-18"))
    assert "Error" not in remote.cognicore_resume_session(_ctx(), session_id="sess_20261116_101010_0961ed90")


# 4. DATE RECALL ------------------------------------------------------------------------

def test_date_parsing():
    today = date(2026, 11, 17)
    assert bf.date_in_query("what did we do yesterday?", today) == date(2026, 11, 16)
    assert bf.date_in_query("what did we do on 2026-11-15?", today) == date(2026, 11, 15)
    assert bf.date_in_query("work of nov 15", today) == date(2026, 11, 15)
    assert bf.date_in_query("15th November", today) == date(2026, 11, 15)
    assert bf.date_in_query("Dec 20", today) == date(2025, 12, 20)
    assert bf.date_in_query("the latest work", today) is None


def test_date_query_lists_that_days_sessions(account):
    out = remote.cognicore_resume_session(_ctx(), query="what did we do on 2026-11-15?")
    assert "Sessions on 2026-11-15: 2" in out
    assert "sess_20261115_051727_8db64038" in out and "sess_20261115_153609_f32d222e" in out
    assert "sess_20261104_074224_50e0ed3e" not in out


def test_day_without_sessions_says_so(account):
    out = remote.cognicore_resume_session(_ctx(), query="what did we do on 2026-11-10?")
    assert "No sessions were saved on 2026-11-10." in out
    assert "Nearest sessions:" in out


def test_session_spanning_days_counts_for_each(account):
    assert "Sessions on 2026-11-16: 1" in remote.cognicore_resume_session(_ctx(), query="what happened on Nov 16")


# 5. CROSS-AGENT ------------------------------------------------------------------------

def test_agent_question_picks_that_agents_session(account):
    out = remote.cognicore_resume_session(_ctx(), query="what did codex do")
    assert "Sessions saved by codex: 1" in out
    assert "Sarvam" in out


def test_recall_by_agent_name(account):
    assert "Sessions saved by codex: 1" in remote.cognicore_recall("Codex", _ctx())
    assert "Sessions saved by claude code: 2" in remote.cognicore_recall("Claude Code", _ctx())
    assert "No sessions saved by chatgpt." in remote.cognicore_recall("ChatGPT", _ctx())


# 6. STALENESS --------------------------------------------------------------------------

def test_finished_tasks_and_contradicted_state_are_dropped(account):
    out = remote.cognicore_resume_session(_ctx(), max_tokens=3000)
    assert "Full suite must pass" not in out
    assert "Publish 0.13.0 to PyPI" in out  # still open
    assert "Nothing is merged" not in out
    assert "Part 5 mostly exists" in out


def test_done_by_word_overlap():
    assert bf.done_by("Full suite must pass (note the 3 known pre-existing failures).",
                      ["In both parts the full suite gives 1470 passed, 7 skipped, and only the 3 known failures."])
    assert not bf.done_by("Publish 0.13.0 to PyPI", ["PR #227 is merged"])


def test_carried_goal_is_not_carried_again(monkeypatch):
    backend = hooks._backend()
    save_session(backend, [{"role": "user", "content": "Our goal is the recall test."}], project="/p",
                 session_key="a", goal_source="")
    with backend._get_conn() as conn:
        conn.execute("UPDATE sessions SET timestamp = timestamp - 5 * 86400")
    assert hooks._continued_goal(backend, "/p", "b", {}) == ""  # 5 days old: not carried


# 7. NOISE ------------------------------------------------------------------------------

def test_noise_left_out_of_brief(account):
    out = remote.cognicore_resume_session(_ctx(), max_tokens=3000)
    for noise in ("CRLF", "Surface it", "PART 2", "Every decision records", "Which stands",
                  "Before you merge", "It's on the local server only"):
        assert noise not in out, noise
    assert "Store the project context in one readable file" in out
    assert "Part 0 is the stable project key" in out


def test_extraction_skips_prompt_spec_and_narration():
    conv = [
        {"role": "user", "content": "PART 2 — decision history with proof.\n"
                                    "Every decision records its source, agent and date.\n"
                                    "We decided to use SQLite because it ships with Python."},
        {"role": "assistant", "content": "The file has CRLF line endings, so I'll switch to the Edit tool. "
                                         "Background saved as a decision: nothing."},
    ]
    doc = build_handoff(conv, "s", "n", time.time())
    whats = [d["what"] for d in doc["decisions"]]
    assert whats == ["We decided to use SQLite"]
    assert doc["decisions"][0]["why"] == "it ships with Python."


def test_naming_the_goal_line_states_no_goal():
    from cognicore.memory.handoff import stated_goal
    assert stated_goal([{"role": "user", "content": "Report: session date, agent, and the Goal line."}]) == ""
    assert stated_goal([{"role": "user", "content": "Our goal is to ship the parser."}])
    assert stated_goal([{"role": "user", "content": "Main goal: fix the brief dates."}])
    assert stated_goal([{"role": "user", "content": "The goal for this session is a clean brief."}])


def test_agent_directed_instruction_is_not_a_project_rule():
    from cognicore.project_suggest import rule_sentences
    conv = [{"role": "user", "content": "You are testing the connector. Never guess or fill gaps from your own memory."},
            {"role": "user", "content": "Never put keys in git."}]
    assert rule_sentences(conv) == ["Never put keys in git."]


# 8. EXACT VALUES -----------------------------------------------------------------------

def test_exact_values_recalled_verbatim(account):
    out = remote.cognicore_recall("PR #227", _ctx())
    assert "Exact matches" in out and "PR #227 (project context) is already merged" in out
    assert "feat/sarvam-tuning" in remote.cognicore_recall("feat/sarvam-tuning", _ctx())
    assert "BLUE-HERON-4417" in remote.cognicore_recall("BLUE-HERON-4417", _ctx())


def test_done_items_are_searchable_rows():
    backend = hooks._backend()
    save_session(backend, supplied_handoff={"goal": "g", "current_state": "Waiting for review.",
                                            "completed": ["Merged PR #301 into main", "Tagged 0.14.0"]})
    with backend._get_conn() as conn:
        assert conn.execute("SELECT category FROM memory_entries WHERE text LIKE '%#301%'").fetchone()[0] == "completed"


# 9. PROJECT LABELS ---------------------------------------------------------------------

def test_session_in_parent_folder_is_labelled_by_its_repo(account):
    out = remote.cognicore_resume_session(_ctx())
    assert "Session: cognicore-my-openenv" in out
    assert display_name({"session_name": "safetymind"}, ["a.py", "b.py"]) == "safetymind"


def test_work_dir_finds_the_repo_below(tmp_path):
    repo = tmp_path / "cognicore-my-openenv"
    (repo / ".git").mkdir(parents=True)
    conv = [{"role": "assistant", "content": f"Edited {repo}/cognicore/hooks.py, cognicore-my-openenv/tests/a.py "
                                             "and cognicore-my-openenv/cognicore/repo.py"}]
    assert hooks.work_dir(str(tmp_path), conv) == str(repo)
    assert hooks.work_dir(str(tmp_path), [{"role": "user", "content": "hello"}]) == str(tmp_path)


# 10. PROJECT CONTEXT -------------------------------------------------------------------

def test_project_context_reaches_hosted_brief(account):
    out = remote.cognicore_resume_session(_ctx())
    assert "Never put keys in git" in out
    assert "Parts 0-1 pushed" in out
    assert out.index("Never put keys in git") < out.index("Session:")


def test_session_dates_from_id_when_created_was_overwritten():
    assert session_dates({"session_id": "sess_20261103_151101_df90683f", "created_at": "2026-11-15T00:00:00Z"}) == \
        ("2026-11-03", "2026-11-15")


def test_resume_without_project_context_unchanged(tmp_path):
    backend = hooks._backend()
    save_session(backend, [{"role": "user", "content": "Our goal is to ship the parser."}])
    assert "Goal: Ship the parser" in resume_session(backend)

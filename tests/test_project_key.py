"""project_key(): one stable key per project, whatever folder the agent is in.

The bug: one Claude Code session was briefed as "safetymind" and then saved as
"cognicore-my-openenv", because the hooks keyed it by the event's cwd, which
follows the agent's `cd`. Now the key is the repository's top-level folder
(worktrees map to the main checkout), else the folder the session started in.
"""
import io
import json
import shutil
import sqlite3
import subprocess  # nosec B404 - runs git with fixed arguments in a temp folder

import pytest

from cognicore import hooks
from cognicore.memory import SQLiteMemoryBackend
from cognicore.memory.context_preservation import _project_key_cached, migrate_project_keys, project_key
from cognicore.memory.transcript import start_cwd
from cognicore.repo import project_folder, repo_root


@pytest.fixture(autouse=True)
def _fresh_cache():
    _project_key_cached.cache_clear()
    yield
    _project_key_cached.cache_clear()


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def test_nested_folders_share_the_repository_key(tmp_path):
    root = _repo(tmp_path / "shop")
    deep = root / "src" / "billing" / "eu"
    deep.mkdir(parents=True)
    assert project_key(str(deep)) == project_key(str(root)) == project_key(str(root / "src"))
    assert repo_root(str(deep)) == root.resolve()


def test_folder_outside_any_repository_is_its_own_project(tmp_path):
    a, b = tmp_path / "notes" / "a", tmp_path / "notes" / "b"
    a.mkdir(parents=True)
    b.mkdir()
    assert repo_root(str(a)) is None
    assert project_key(str(a)) != project_key(str(b))
    assert project_folder(str(a)) == a.resolve()


def test_nested_repository_wins_over_the_outer_one(tmp_path):
    outer = _repo(tmp_path / "mono")
    inner = _repo(outer / "vendor" / "lib")
    assert project_key(str(inner / "pkg")) == project_key(str(inner)) != project_key(str(outer))


def test_home_folder_repository_does_not_swallow_projects(tmp_path, monkeypatch):
    home = _repo(tmp_path / "home")  # a dotfiles repo at ~, as on this author's machine
    work = home / "Documents" / "safetymind"
    work.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    assert repo_root(str(work)) is None
    assert project_key(str(work)) != project_key(str(home))
    assert repo_root(str(home)) == home.resolve()  # the home folder itself is still a repo


def test_linked_worktree_maps_to_the_main_checkout(tmp_path):
    main = _repo(tmp_path / "app")
    gitdir = main / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    tree = tmp_path / "app-feature"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert repo_root(str(tree / "src")) == tree.resolve()  # the checkout itself, for .cognicore/context.md
    assert project_key(str(tree / "src")) == project_key(str(main))


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_real_git_worktree(tmp_path):
    main = tmp_path / "real"
    main.mkdir()
    run = dict(cwd=main, check=True, capture_output=True)
    subprocess.run(["git", "init", "-q"], **run)  # nosec B603 B607
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",  # nosec B603 B607
                    "commit", "-q", "--allow-empty", "-m", "init"], **run)
    subprocess.run(["git", "worktree", "add", "-q", str(tmp_path / "real-wt")], **run)  # nosec B603 B607
    assert project_key(str(tmp_path / "real-wt")) == project_key(str(main))


def test_submodule_and_broken_git_files_are_their_own_root(tmp_path):
    sub = tmp_path / "super" / "sub"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")  # no commondir
    assert project_folder(str(sub)) == sub.resolve()
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / ".git").write_text("not a gitdir line", encoding="utf-8")
    assert project_folder(str(junk / ".")) == junk.resolve()


def test_empty_and_missing_paths(tmp_path):
    assert project_key("") == ""
    missing = tmp_path / "gone" / "away"
    assert project_key(str(missing)) == project_key(str(missing))  # stable, no crash


# --- the starting folder names the session ---------------------------------------------

def _transcript(path, cwds):
    lines = [json.dumps({"type": "user", "cwd": c, "message": {"role": "user", "content": f"step in {c}: "
             "We decided to use Decimal because finance requires commercial rounding."}}) for c in cwds]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_start_cwd_reads_the_first_folder(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text('not json\n{"type":"summary"}\n{"cwd": "/first"}\n{"cwd": "/second"}\n', encoding="utf-8")
    assert start_cwd(str(t)) == "/first"
    codex = tmp_path / "c.jsonl"
    codex.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": "/codex"}}) + "\n", encoding="utf-8")
    assert start_cwd(str(codex)) == "/codex"
    assert start_cwd(str(tmp_path / "missing.jsonl")) == ""
    (tmp_path / "e.jsonl").write_text('{"cwd": 5}\n[1,2]\n', encoding="utf-8")
    assert start_cwd(str(tmp_path / "e.jsonl")) == ""


def _run(command, event, monkeypatch, data_dir):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(data_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hooks.main([command]) == 0
    return out.getvalue()


def test_session_that_cds_into_a_subproject_keeps_one_key_and_name(tmp_path, monkeypatch):
    """The reported bug: briefed as "safetymind", later saved as "cognicore-my-openenv"."""
    outer = tmp_path / "safetymind"
    inner = _repo(outer / "cognicore-my-openenv")
    transcript = _transcript(tmp_path / "t.jsonl", [str(outer), str(inner), str(inner)])
    for _ in range(2):  # Stop fires every turn; by then the event's cwd is the inner repo
        _run("save", {"session_id": "s-1", "transcript_path": str(transcript), "cwd": str(inner)},
             monkeypatch, tmp_path / "mem")
    rows = sqlite3.connect(tmp_path / "mem" / "mcp_memories.db").execute(
        "SELECT project, name FROM sessions").fetchall()
    assert rows == [(project_key(str(outer)), "safetymind")]
    assert _run("session-start", {"cwd": str(outer), "source": "startup"}, monkeypatch, tmp_path / "mem")


def test_session_started_in_a_subfolder_is_the_repository(tmp_path, monkeypatch):
    root = _repo(tmp_path / "billing-app")
    sub = root / "src" / "eu"
    sub.mkdir(parents=True)
    transcript = _transcript(tmp_path / "t.jsonl", [str(sub)])
    _run("save", {"session_id": "s-2", "transcript_path": str(transcript), "cwd": str(sub)},
         monkeypatch, tmp_path / "mem")
    rows = sqlite3.connect(tmp_path / "mem" / "mcp_memories.db").execute(
        "SELECT project, name FROM sessions").fetchall()
    assert rows == [(project_key(str(root)), "billing-app")]
    out = _run("session-start", {"cwd": str(root), "source": "startup"}, monkeypatch, tmp_path / "mem")
    assert "finance requires commercial rounding" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_transcript_without_cwd_falls_back_to_the_event(tmp_path, monkeypatch):
    root = _repo(tmp_path / "app")
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user", "message": {"role": "user", "content":
                 "We decided to use Postgres because the team knows it."}}) + "\n", encoding="utf-8")
    _run("save", {"session_id": "s-3", "transcript_path": str(t), "cwd": str(root / "sub")},
         monkeypatch, tmp_path / "mem")
    rows = sqlite3.connect(tmp_path / "mem" / "mcp_memories.db").execute("SELECT project FROM sessions").fetchall()
    assert rows == [(project_key(str(root)),)]


# --- migration of records saved with the old keys ---------------------------------------

def _old_db(path, projects):
    SQLiteMemoryBackend(str(path))  # current schema, marker written on the empty table
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM cognicore_meta")
    conn.executemany("INSERT INTO sessions (session_id, name, timestamp, project) VALUES (?, ?, ?, ?)",
                     [(f"s{i}", f"n{i}", i, p) for i, p in enumerate(projects)])
    conn.commit()
    conn.close()


def test_migration_rekeys_subfolder_sessions_without_losing_any(tmp_path):
    root = _repo(tmp_path / "shop")
    (root / "src").mkdir()
    db = tmp_path / "mem.db"
    from cognicore.memory.context_preservation import _normalized
    foreign = "c:\\users\\someone-else\\project"  # synced from another machine: must stay verbatim
    _old_db(db, [_normalized(str(root / "src")), _normalized(str(root)), foreign, ""])
    SQLiteMemoryBackend(str(db))  # opening the database runs the migration once
    conn = sqlite3.connect(db)
    projects = [r[0] for r in conn.execute("SELECT project FROM sessions ORDER BY session_id")]
    assert projects == [project_key(str(root)), project_key(str(root)), foreign, ""]
    assert conn.execute("SELECT value FROM cognicore_meta WHERE key = 'project_keys_v2:sessions'").fetchone()


def test_migration_runs_once(tmp_path):
    root = _repo(tmp_path / "shop")
    (root / "src").mkdir()
    db = tmp_path / "mem.db"
    from cognicore.memory.context_preservation import _normalized
    _old_db(db, [_normalized(str(root / "src"))])
    conn = sqlite3.connect(db)
    assert migrate_project_keys(conn) == 1
    conn.execute("UPDATE sessions SET project = 'manual-edit'")
    assert migrate_project_keys(conn) == 0  # marker set: a later run never touches rows again
    assert conn.execute("SELECT project FROM sessions").fetchone()[0] == "manual-edit"


def test_migration_rejects_unknown_columns(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    with pytest.raises(ValueError):
        migrate_project_keys(conn, table="sessions; DROP TABLE x", column="project")


def test_lessons_scope_is_migrated(tmp_path, monkeypatch):
    from cognicore import lessons
    from cognicore.memory.context_preservation import _normalized

    root = _repo(tmp_path / "shop")
    (root / "src").mkdir()
    db = tmp_path / "lessons.db"
    conn = lessons._connect(db)
    conn.execute("DELETE FROM cognicore_meta")
    now = 1.0
    conn.execute("INSERT INTO lessons (mistake, fix, scope, created, updated) VALUES (?, ?, ?, ?, ?)",
                 ("used float for money", "use Decimal", _normalized(str(root / "src")), now, now))
    conn.execute("INSERT INTO lessons (mistake, fix, scope, created, updated) VALUES (?, ?, ?, ?, ?)",
                 ("x", "y", "global", now, now))
    conn.commit()
    conn.close()
    scopes = sorted(r[0] for r in lessons._connect(db).execute("SELECT scope FROM lessons"))
    assert scopes == sorted([project_key(str(root)), "global"])


def test_no_network_is_touched(tmp_path, monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    root = _repo(tmp_path / "offline")
    assert project_key(str(root / "a" / "b")) == project_key(str(root))

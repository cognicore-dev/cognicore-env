"""
Lessons: mistakes an agent made, what fixed them, and why. Checked before acting.

    cognicore lesson add --mistake "..." --fix "..." --why "..." [--when "..."] [--scope project|global]
    cognicore lesson check "<what I'm about to do>"   # top 3 relevant lessons, <= ~200 tokens, or nothing
    cognicore lesson list [--all]
    cognicore lesson forget <id>

Writing lessons down is easy; the hard part is using them. ``check`` returns
only the few lessons that match the task at hand, within a small token budget,
and prints nothing when nothing matches, so it costs nothing on unrelated work.

- A lesson similar to an existing one is merged into it and counted
  ("seen 3x") instead of piling up.
- Every field is passed through CogniCore's secret redaction.
- Lessons live in ``lessons.db`` in the CogniCore data directory
  (``$COGNICORE_DATA_DIR``, default ``~/.cognicore/mcp``). Nothing is sent anywhere.
- ``--scope project`` (the default) ties a lesson to the current folder;
  ``global`` lessons apply everywhere.

Matching is lexical (SQLite FTS5, BM25 ranking, prefix match on each word),
so a lesson is found when the task shares words with it. ``--when`` names the
situation in general words ("installing packages") so the lesson also matches
tasks phrased differently ("add axios").
"""

import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

DB_NAME = "lessons.db"
CHECK_LIMIT = 3
CHECK_TOKENS = 200
MERGE_SIMILARITY = 0.6
MIN_RELATIVE_SCORE = 0.5  # bm25 scores are negative: keep rank <= best * 0.5
MAX_FIELD = 300

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "i", "in", "into", "is", "it",
    "me", "my", "of", "on", "or", "our", "please", "so", "that", "the", "then", "this", "to", "up", "we",
    "with", "you", "your", "about", "going", "will", "want", "need", "now", "some", "can", "just",
}


def _data_dir() -> Path:
    raw = os.environ.get("COGNICORE_DATA_DIR", "")
    path = Path(raw) if raw else Path.home() / ".cognicore" / "mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_key(path: Optional[str] = None) -> str:
    from cognicore.memory.context_preservation import project_key

    return project_key(path or os.getcwd())


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or _data_dir() / DB_NAME))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mistake TEXT NOT NULL,
            fix TEXT NOT NULL,
            why TEXT NOT NULL DEFAULT '',
            situation TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL,              -- 'global' or a normalized project folder
            seen INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL,
            updated REAL NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(mistake, fix, why, situation);
    """)
    return conn


def _reindex(conn: sqlite3.Connection, row_id: int) -> None:
    row = conn.execute("SELECT * FROM lessons WHERE id = ?", (row_id,)).fetchone()
    conn.execute("DELETE FROM lessons_fts WHERE rowid = ?", (row_id,))
    if row:
        conn.execute("INSERT INTO lessons_fts(rowid, mistake, fix, why, situation) VALUES (?, ?, ?, ?, ?)",
                     (row_id, row["mistake"], row["fix"], row["why"], row["situation"]))


def _clean(text: str) -> str:
    from cognicore.memory.redact import redact_secrets

    text = " ".join(str(text or "").split())
    return redact_secrets(text[:MAX_FIELD])[0]


def _words(text: str) -> Set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_.\-]*", text.lower()) if w not in _STOPWORDS and len(w) > 1}


# Words that change how a lesson reads, not what it says.
_FILLER = {"not", "instead", "here", "there", "repo", "project", "always", "never", "dont", "don't", "use", "used",
           "using", "ran", "run", "running", "should", "must", "rather", "than", "only", "also", "in", "this"}


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "s"):  # not "es": "dates" -> "dat" would match DATABASE_URL
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _key_words(text: str) -> Set[str]:
    return {_stem(w) for w in _words(text) if w not in _FILLER}


def _similar(a: str, b: str) -> float:
    wa, wb = _key_words(a), _key_words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def add(mistake: str, fix: str, why: str = "", when: str = "", scope: str = "project",
        project: Optional[str] = None, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Record a lesson, or merge it into a very similar one in the same scope. Returns {id, merged, seen}."""
    mistake, fix, why, when = _clean(mistake), _clean(fix), _clean(why), _clean(when)
    if not mistake or not fix:
        raise ValueError("a lesson needs both --mistake and --fix")
    key = "global" if scope == "global" else _project_key(project)
    now = time.time()
    with _connect(db_path) as conn:
        best, best_score = None, 0.0
        for row in conn.execute("SELECT * FROM lessons WHERE scope = ?", (key,)):
            # The same fix is the same lesson, however the mistake was worded.
            score = max(_similar(fix, row["fix"]), _similar(f"{mistake} {fix}", f"{row['mistake']} {row['fix']}"))
            if score > best_score:
                best, best_score = row, score
        if best is not None and best_score >= MERGE_SIMILARITY:
            conn.execute(
                "UPDATE lessons SET seen = seen + 1, updated = ?, why = CASE WHEN length(?) > length(why) THEN ? ELSE why END, "
                "situation = CASE WHEN ? != '' THEN ? ELSE situation END WHERE id = ?",
                (now, why, why, when, when, best["id"]))
            _reindex(conn, best["id"])
            return {"id": best["id"], "merged": True, "seen": best["seen"] + 1}
        cur = conn.execute(
            "INSERT INTO lessons (mistake, fix, why, situation, scope, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mistake, fix, why, when, key, now, now))
        _reindex(conn, cur.lastrowid)
        return {"id": cur.lastrowid, "merged": False, "seen": 1}


def check(task: str, project: Optional[str] = None, limit: int = CHECK_LIMIT, max_tokens: int = CHECK_TOKENS,
          db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The few lessons relevant to *task* (this project's and global ones), best first, within the budget."""
    terms = sorted({_stem(w) for w in _words(task)})  # "vendored" also finds "vendor/"
    if not terms:
        return []
    query = " OR ".join('"' + t.replace('"', "") + '"*' for t in terms)
    scopes = ("global", _project_key(project))
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT l.*, bm25(lessons_fts) AS rank FROM lessons_fts JOIN lessons l ON l.id = lessons_fts.rowid "
            "WHERE lessons_fts MATCH ? AND l.scope IN (?, ?) ORDER BY rank LIMIT 20",
            (query, *scopes)).fetchall()
    # bm25() is lower-is-better; lessons seen more often get a small boost.
    ranked = sorted(rows, key=lambda r: r["rank"] - 0.5 * (r["seen"] - 1))
    if not ranked:
        return []
    # Show only lessons at least half as relevant as the best one: a single shared
    # word ("package", "format") shouldn't bring an unrelated lesson along.
    best = ranked[0]["rank"]
    ranked = [r for r in ranked if r["rank"] <= best * MIN_RELATIVE_SCORE]
    picked, used = [], 0
    for row in ranked:
        line = format_line(row)
        cost = len(line) // 4 + 1
        if len(picked) == limit or (picked and used + cost > max_tokens):
            break
        picked.append(dict(row))
        used += cost
    return picked


def format_line(row: Any) -> str:
    why = f" (why: {row['why']})" if row["why"] else ""
    seen = f" [seen {row['seen']}x]" if row["seen"] > 1 else ""
    return f"- {row['fix']}{why}. Avoid: {row['mistake']}{seen} #{row['id']}"


def list_lessons(project: Optional[str] = None, include_all: bool = False,
                 db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with _connect(db_path) as conn:
        if include_all:
            rows = conn.execute("SELECT * FROM lessons ORDER BY updated DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM lessons WHERE scope IN (?, ?) ORDER BY updated DESC",
                                ("global", _project_key(project))).fetchall()
    return [dict(r) for r in rows]


def forget(lesson_id: int, db_path: Optional[Path] = None) -> bool:
    with _connect(db_path) as conn:
        gone = conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,)).rowcount > 0
        conn.execute("DELETE FROM lessons_fts WHERE rowid = ?", (lesson_id,))
    return gone


def main(argv: List[str]) -> int:
    """Entry point for ``cognicore lesson ...`` (argv after "lesson")."""
    import argparse

    p = argparse.ArgumentParser(prog="cognicore lesson", description="Lessons from mistakes, checked before acting.")
    sub = p.add_subparsers(dest="action", required=True)
    a = sub.add_parser("add", help="Record what went wrong, what works, and why")
    a.add_argument("--mistake", required=True, help="What went wrong, in one sentence")
    a.add_argument("--fix", required=True, help="What works instead, in one sentence")
    a.add_argument("--why", default="", help="The reason, in one sentence")
    a.add_argument("--when", default="", help='The situation in general words, e.g. "installing packages"')
    a.add_argument("--scope", choices=["project", "global"], default="project")
    c = sub.add_parser("check", help="Print the lessons relevant to a task (nothing if none)")
    c.add_argument("task", nargs="+", help="One line describing what you are about to do")
    ls = sub.add_parser("list", help="Show this project's and global lessons")
    ls.add_argument("--all", action="store_true", help="Lessons from every project")
    f = sub.add_parser("forget", help="Delete a lesson")
    f.add_argument("id", type=int)
    args = p.parse_args(argv)

    if args.action == "add":
        try:
            res = add(args.mistake, args.fix, args.why, args.when, args.scope)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        word = f"merged into lesson #{res['id']} (seen {res['seen']}x)" if res["merged"] else f"saved as lesson #{res['id']}"
        print(f"lesson {word}")
        return 0
    if args.action == "check":
        rows = check(" ".join(args.task))
        if rows:
            print("Lessons from earlier mistakes (apply them):")
            for row in rows:
                print(format_line(row))
        return 0
    if args.action == "list":
        rows = list_lessons(include_all=args.all)
        if not rows:
            print("No lessons yet.")
        for row in rows:
            where = "global" if row["scope"] == "global" else Path(row["scope"]).name
            print(f"{format_line(row)}  ({where})")
        return 0
    if args.action == "forget":
        ok = forget(args.id)
        print(f"lesson #{args.id} deleted" if ok else f"no lesson #{args.id}")
        return 0 if ok else 1
    return 2

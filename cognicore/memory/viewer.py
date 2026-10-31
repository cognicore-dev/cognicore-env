"""
Read-only local viewer for saved sessions.

    python -m cognicore.memory.viewer [--db PATH] [--port 37780]

Lists sessions by project (newest first) with goal, agent and open tasks, and
shows each session's resume brief and stored handoff. Binds to 127.0.0.1 only
and never writes to the database. Standard library only.
"""

import argparse
import html
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STYLE = """
:root { --bg:#fbfaf8; --fg:#1f1e1c; --muted:#6b6862; --line:#e4e1db; --card:#ffffff; --accent:#c4602c; }
@media (prefers-color-scheme: dark) { :root { --bg:#191817; --fg:#ecebe8; --muted:#a19d96; --line:#33312e;
  --card:#222120; --accent:#e08a5b; } }
body { margin:0; font:15px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
main { max-width:960px; margin:0 auto; padding:24px 16px; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:15px; margin:28px 0 8px; color:var(--muted); word-break:break-all; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; margin:8px 0; }
.meta { color:var(--muted); font-size:13px; } pre { white-space:pre-wrap; word-break:break-word; font-size:13px;
  background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; overflow-x:auto; }
"""


def _default_db() -> Path:
    raw = os.environ.get("COGNICORE_DATA_DIR", "")
    return (Path(raw) if raw else Path.home() / ".cognicore" / "mcp") / "mcp_memories.db"


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width, initial-scale=1'><title>{html.escape(title)}</title>"
            f"<style>{STYLE}</style></head><body><main>{body}</main></body></html>").encode("utf-8")


def render_index(db: Path) -> bytes:
    with _connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        project_col = "project" if "project" in cols else "NULL AS project"
        handoff_col = "handoff_json" if "handoff_json" in cols else "NULL AS handoff_json"
        rows = conn.execute(f"SELECT session_id, name, timestamp, {project_col}, {handoff_col} "  # nosec B608 - fixed columns
                            "FROM sessions ORDER BY timestamp DESC LIMIT 500").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["project"] or "(no project)", []).append(r)
    parts = [f"<h1>CogniCore memory</h1><p class='meta'>{len(rows)} sessions &middot; {html.escape(str(db))}</p>"]
    for project, sessions in groups.items():
        parts.append(f"<h2>{html.escape(project)}</h2>")
        for r in sessions:
            doc = json.loads(r["handoff_json"]) if r["handoff_json"] else {}
            prov = doc.get("provenance", {})
            tasks = [t["task"] for t in doc.get("open_tasks", []) if t.get("status") == "open"]
            parts.append(
                f"<div class='card'><a href='/session?id={html.escape(r['session_id'], quote=True)}'>"
                f"{html.escape(r['name'] or r['session_id'])}</a>"
                f"<div class='meta'>{html.escape(prov.get('created_at', ''))} "
                f"{html.escape(prov.get('agent', ''))} &middot; {len(doc.get('decisions', []))} decisions &middot; "
                f"{len(tasks)} open tasks</div>"
                f"<div>{html.escape(doc.get('goal', '') or '(pre-v1 session)')}</div></div>")
    return _page("CogniCore memory", "".join(parts))


def render_session(db: Path, session_id: str) -> bytes:
    from cognicore.memory.handoff import handoff_from_legacy, render_text, select_for_budget, text_cost

    with _connect(db) as conn:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return _page("Not found", "<p>No such session. <a href='/'>Back</a></p>")
        raw = row["handoff_json"] if "handoff_json" in row.keys() else None
        if raw:
            doc = json.loads(raw)
        else:  # pre-v1 session: rebuilt from its memory rows, as resume does (nothing written back)
            mem = conn.execute("SELECT text, category FROM memory_entries WHERE session_id = ? ORDER BY entry_id",
                               (session_id,)).fetchall()
            doc = handoff_from_legacy([dict(m) for m in mem], session_id, row["name"], row["timestamp"] or 0.0)
    # Same brief resume_session builds, unbudgeted, without opening the backend
    # (its startup runs schema migrations, which would write to the file).
    sections = {
        "goal": doc.get("goal", ""), "current_state": doc.get("current_state", ""),
        "open_tasks": [t for t in doc.get("open_tasks", []) if t.get("status") == "open"],
        "decisions": list(reversed(doc.get("decisions", []))), "key_facts": doc.get("key_facts", []),
        "files": doc.get("files", []), "code": doc.get("code", []),
    }
    included, _ = select_for_budget(sections, None, text_cost)
    brief = render_text(included, doc.get("provenance", {}))
    handoff = json.dumps(doc, indent=2, ensure_ascii=False)
    body = (f"<p><a href='/'>&larr; All sessions</a></p><h1>{html.escape(row['name'] or session_id)}</h1>"
            f"<p class='meta'>{html.escape(session_id)}</p><h2>Resume brief</h2><pre>{html.escape(brief)}</pre>"
            + (f"<h2>Stored handoff</h2><pre>{html.escape(handoff)}</pre>" if handoff else ""))
    return _page(row["name"] or session_id, body)


def make_handler(db: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path == "/":
                    body, status = render_index(db), 200
                elif url.path == "/session":
                    body, status = render_session(db, parse_qs(url.query).get("id", [""])[0]), 200
                else:
                    body, status = _page("Not found", "<p>Not found.</p>"), 404
            except sqlite3.Error as e:
                body, status = _page("Error", f"<p>Could not read {html.escape(str(db))}: {html.escape(str(e))}</p>"), 500
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # quiet
            pass

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only viewer for CogniCore sessions")
    parser.add_argument("--db", type=Path, default=_default_db())
    parser.add_argument("--port", type=int, default=37780)
    args = parser.parse_args(argv)
    if not args.db.exists():
        raise SystemExit(f"No memory database at {args.db}")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.db))
    print(f"CogniCore memory viewer: http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

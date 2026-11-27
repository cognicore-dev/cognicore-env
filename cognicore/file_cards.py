"""File cards: what agents learned about a project's files, kept across sessions.

Agents re-read the same files in every new session (and after a context compaction) to learn
their layout again, which costs thousands of tokens each time. A file card keeps, per project
and file, what is cheap to keep and quick to use instead:

    path, size, line count, SHA-256 of the content
    outline     names and line numbers of its classes, functions, headings or top-level keys
                (never values), so an agent can read only the lines it needs
    note        one line an agent wrote about the file ("edit _verify_redirect for new providers")
    sessions    how many sessions read or edited it, and when it was last used

Cards are built at session save, from the transcript (files read or edited) and the files on
disk: no model calls. The startup brief lists the most used ones in whatever room its token
budget has left. When a file changed since its card was made, the card says so and its outline
is rebuilt, so a stale outline is never presented as current.

Secret-looking files (.env, keys, certificates, credentials) and large or binary files get no
card. Everything stays in the local memory database. COGNICORE_FILE_CARDS=0 turns it off.
"""

import ast
import fnmatch
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_OUTLINE = 40           # entries kept per file
NOTE_CHARS = 300
BRIEF_FILES = 5
BRIEF_TOKENS = 150         # most the startup brief spends on file cards
SECRET_NAMES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
                "*credentials*", "*secret*", "*.keystore", ".npmrc", ".pypirc", ".netrc")

_READ_TOOLS = {"Read"}
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update) File: (.+?)\s*$", re.M)
# Shell commands Codex uses to read files: cat, nl, head, tail, sed -n, less, Get-Content, type.
_SHELL_READ = re.compile(
    r"(?:^|[;&|(\"'`]\s*|\s)(?:cat|nl(?:\s+-ba)?|head(?:\s+-n\s*\d+|\s+-\d+)?|tail(?:\s+-n\s*\d+|\s+-\d+)?|"
    r"sed\s+-n\s+(?:'[^']*'|\"[^\"]*\"|\S+)|less|Get-Content|gc|type)\s+(?:-\S+\s+)*['\"]?([^\s'\";|&<>()]+)",
    re.I)


def enabled() -> bool:
    return os.environ.get("COGNICORE_FILE_CARDS", "1").strip() != "0"


def _root(project_dir: str) -> Path:
    """The repository's top folder (else the folder itself), as for session project keys."""
    from cognicore.repo import project_folder

    try:
        return Path(project_folder(project_dir))
    except (OSError, RuntimeError, ValueError):
        return Path(project_dir)


def _schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS file_cards (
        project TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT, size INTEGER, lines INTEGER,
        outline TEXT, note TEXT DEFAULT '', sessions INTEGER DEFAULT 0, last_used REAL,
        PRIMARY KEY (project, path))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS file_card_sessions (
        project TEXT NOT NULL, path TEXT NOT NULL, session_key TEXT NOT NULL,
        PRIMARY KEY (project, path, session_key))""")


# --- which files a session used -----------------------------------------------------------------

def _rel(path: str, root: Path) -> Optional[str]:
    """*path* relative to *root* with forward slashes, or None when it is outside the project."""
    try:
        p = Path(path)
        p = (root / p) if not p.is_absolute() else p
        rel = p.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return rel.as_posix()


def files_used(transcript: str, root: Path) -> Dict[str, Dict[str, int]]:
    """{relative path: {"reads": n, "edits": n}} for project files a session read or edited."""
    from cognicore.memory.transcript import _entries

    used: Dict[str, Dict[str, int]] = {}

    def add(path: str, kind: str):
        rel = _rel(path, root)
        if rel and not rel.startswith(".."):
            used.setdefault(rel, {"reads": 0, "edits": 0})[kind] += 1

    for entry in _entries(transcript):
        if entry.get("type") == "assistant":  # Claude Code
            content = (entry.get("message") or {}).get("content") or []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                args = block.get("input") or {}
                target = args.get("file_path") or args.get("notebook_path") if isinstance(args, dict) else None
                if target and block.get("name") in _READ_TOOLS:
                    add(str(target), "reads")
                elif target and block.get("name") in _EDIT_TOOLS:
                    add(str(target), "edits")
        elif entry.get("type") == "response_item":  # Codex
            item = entry.get("payload") or {}
            if item.get("type") not in ("function_call", "custom_tool_call", "local_shell_call"):
                continue
            raw = item.get("arguments") if "arguments" in item else item.get("input")
            if isinstance(raw, str) and raw.strip().startswith("{"):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    pass
            if isinstance(raw, dict):
                raw = "\n".join(v if isinstance(v, str) else " ".join(map(str, v)) if isinstance(v, list) else ""
                                for v in raw.values())
            if not isinstance(raw, str):  # local_shell_call: {"action": {"command": ["bash", "-lc", "..."]}}
                command = (item.get("action") or {}).get("command") if isinstance(item.get("action"), dict) else None
                raw = " ".join(map(str, command)) if isinstance(command, list) else json.dumps(item.get("action") or "")
            body = raw
            for target in _PATCH_FILE.findall(body):
                add(target, "edits")
            for target in _SHELL_READ.findall(body):
                add(target, "reads")
    return used


# --- outlines --------------------------------------------------------------------------------

def _secret_like(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_NAMES)


def _python_outline(text: str) -> List[str]:
    tree = ast.parse(text)
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.append(f"{node.lineno}: class {node.name}")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(f"{sub.lineno}:   def {sub.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(f"{node.lineno}: def {node.name}")
    return out


_CODE_PATTERNS = [
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*=>"),
    re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+([A-Za-z_][\w]*)"),
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|record)\s+([A-Za-z_]\w*)"),
]
_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*$")
_TOP_KEY = re.compile(r"^([A-Za-z_][\w.-]*)\s*[:=]")
_TOML_TABLE = re.compile(r"^\[{1,2}([^\]]+)\]{1,2}\s*$")


def outline(rel: str, text: str) -> List[str]:
    """Line-numbered names (definitions, headings or top-level keys) in the file; never values."""
    suffix = Path(rel).suffix.lower()
    lines = text.splitlines()
    out: List[str] = []
    if suffix in (".py", ".pyi"):
        try:
            out = _python_outline(text)
        except (SyntaxError, ValueError):
            out = []
    elif suffix in (".md", ".markdown", ".rst"):
        out = [f"{i}: {m.group(1)} {m.group(2)}" for i, ln in enumerate(lines, 1) if (m := _HEADING.match(ln))]
    elif suffix == ".json":
        try:
            data = json.loads(text)
            out = [f"key {k}" for k in data] if isinstance(data, dict) else []
        except ValueError:
            out = []
    elif suffix in (".yml", ".yaml", ".toml", ".ini", ".cfg"):
        for i, ln in enumerate(lines, 1):
            if (m := _TOML_TABLE.match(ln)):
                out.append(f"{i}: [{m.group(1)}]")
            elif ln and not ln[0].isspace() and (m := _TOP_KEY.match(ln)):
                out.append(f"{i}: {m.group(1)}")
    else:
        for i, ln in enumerate(lines, 1):
            for pattern in _CODE_PATTERNS:
                if (m := pattern.match(ln)):
                    out.append(f"{i}: {ln.strip().split('(')[0].split('{')[0].split('=')[0].strip()[:80]}")
                    break
    if len(out) > MAX_OUTLINE:
        out = out[:MAX_OUTLINE] + [f"... {len(out) - MAX_OUTLINE} more"]
    return out


def _read_file(path: Path) -> Optional[Tuple[str, bytes]]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="replace"), data


# --- storing and reading cards -------------------------------------------------------------------

def _refresh(conn, project: str, rel: str, root: Path) -> Optional[Dict[str, Any]]:
    """Update the card's hash, size, lines and outline from disk; return what changed."""
    if _secret_like(rel):
        return None
    got = _read_file(root / rel)
    if got is None:
        return None
    text, data = got
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute("SELECT sha256 FROM file_cards WHERE project = ? AND path = ?", (project, rel)).fetchone()
    changed = bool(row) and row[0] != sha
    if not row or changed:
        conn.execute(
            "INSERT INTO file_cards (project, path, sha256, size, lines, outline, last_used) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(project, path) DO UPDATE SET sha256=excluded.sha256, size=excluded.size, "
            "lines=excluded.lines, outline=excluded.outline",
            (project, rel, sha, len(data), text.count("\n") + (0 if text.endswith("\n") else 1),
             json.dumps(outline(rel, text)), time.time()))
    return {"changed": changed, "new": not row}


def capture(backend, transcript: str, project_dir: str, session_key: str) -> int:
    """Create or update cards for the files this session used. Returns how many it touched."""
    if not enabled() or not transcript or not project_dir:
        return 0
    from cognicore.memory.context_preservation import project_key

    root = _root(project_dir)
    project = project_key(project_dir)
    used = files_used(transcript, root)
    touched = 0
    with backend._get_conn() as conn:
        _schema(conn)
        for rel in used:
            if _refresh(conn, project, rel, root) is None:
                continue
            first = conn.execute(
                "INSERT OR IGNORE INTO file_card_sessions (project, path, session_key) VALUES (?, ?, ?)",
                (project, rel, session_key or transcript)).rowcount
            conn.execute("UPDATE file_cards SET last_used = ?, sessions = sessions + ? WHERE project = ? AND path = ?",
                         (time.time(), first, project, rel))
            touched += 1
    return touched


def card(backend, project_dir: str, path: str) -> Optional[Dict[str, Any]]:
    """The file's card, refreshed from disk first (so a changed file never shows a stale outline)."""
    from cognicore.memory.context_preservation import project_key

    root = _root(project_dir)
    rel = _rel(path, root)
    if not rel:
        return None
    project = project_key(project_dir)
    with backend._get_conn() as conn:
        _schema(conn)
        state = _refresh(conn, project, rel, root)
        row = conn.execute("SELECT path, size, lines, outline, note, sessions, last_used FROM file_cards "
                           "WHERE project = ? AND path = ?", (project, rel)).fetchone()
    if not row or state is None:
        return None
    return {"path": row[0], "size": row[1], "lines": row[2], "outline": json.loads(row[3] or "[]"),
            "note": row[4] or "", "sessions": row[5], "last_used": row[6],
            "changed_since_last_session": state["changed"]}


def set_note(backend, project_dir: str, path: str, note: str) -> Optional[str]:
    """Attach a one-line note to a file's card (secrets are redacted). Returns the stored note."""
    from cognicore.memory.redact import redact_secrets

    if card(backend, project_dir, path) is None:
        return None
    from cognicore.memory.context_preservation import project_key

    text, _ = redact_secrets(" ".join((note or "").split())[:NOTE_CHARS])
    with backend._get_conn() as conn:
        conn.execute("UPDATE file_cards SET note = ? WHERE project = ? AND path = ?",
                     (text, project_key(project_dir), _rel(path, _root(project_dir))))
    return text


def brief_block(backend, project_dir: str, max_tokens: int = BRIEF_TOKENS) -> str:
    """Most used files with their outlines, for the startup brief, in at most *max_tokens*."""
    from cognicore.memory.context_preservation import TokenBudget, project_key

    if not enabled() or max_tokens < 30:
        return ""
    root, project = _root(project_dir), project_key(project_dir)
    with backend._get_conn() as conn:
        _schema(conn)
        rows = conn.execute("SELECT path FROM file_cards WHERE project = ? ORDER BY sessions DESC, last_used DESC "
                            "LIMIT ?", (project, BRIEF_FILES * 2)).fetchall()
        lines = ["Files used in earlier sessions (outline line numbers, to read only what is needed):"]
        for (rel,) in rows:
            state = _refresh(conn, project, rel, root)
            if state is None:
                continue  # deleted, unreadable or now secret-looking
            n, outline_json, note, sessions = conn.execute(
                "SELECT lines, outline, note, sessions FROM file_cards WHERE project = ? AND path = ?",
                (project, rel)).fetchone()
            names = "; ".join(json.loads(outline_json or "[]")[:6])
            head = f"- {rel} ({n} lines, {sessions} session{'s' if sessions != 1 else ''}"
            head += ", changed since last session)" if state["changed"] else ")"
            entry = head + (f": {note}" if note else "") + (f"\n  {names}" if names else "")
            if TokenBudget.estimate_tokens("\n".join(lines + [entry])) > max_tokens:
                break
            lines.append(entry)
            if len(lines) > BRIEF_FILES:
                break
    return "\n".join(lines) if len(lines) > 1 else ""

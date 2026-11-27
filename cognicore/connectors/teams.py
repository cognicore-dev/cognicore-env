"""Team memory on the hosted server: a shared memory a team's agents read and write.

A team has its own memory database (never mixed with anyone's personal one) and a row in the
team registry (``teams.db``) listing its members and their roles:

    owner    manages members and invites, deletes any team memory, deletes the team
    editor   shares memories to the team, deletes the ones they shared
    viewer   reads only

People join with a single-use invite code that expires (an owner creates it and passes it on
however they like), so nobody needs another person's account id. Codes are stored hashed.

Every operation takes the signed-in user from the request, never from an argument, and checks
membership first; a team the user is not in is reported as not found, so its existence is not
revealed. Every share, deletion, invite, join and membership change is written to the team's
audit log. Text shared to a team has secrets redacted first.

Ids of team memories in recall look like ``team:<team_id>:<entry_id>``.
"""

import hashlib
import secrets
import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROLES = ("owner", "editor", "viewer")
INVITE_TTL = 7 * 24 * 3600
MAX_OWNED_TEAMS = 20
MAX_MEMBERS = 50
MAX_OPEN_INVITES = 20
NAME_CHARS = 60


class TeamError(ValueError):
    """A team operation that is not allowed or not possible; the message is safe to show."""


NOT_FOUND = "No team by that name or id among your teams."


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Teams:
    """The team registry and team memories under *data_dir* (the hosted server's data folder)."""

    def __init__(self, data_dir: Path, provider=None):
        self.data_dir = Path(data_dir)
        self.provider = provider
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS teams (
                    team_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_by TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS members (
                    team_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, name TEXT DEFAULT '',
                    added REAL NOT NULL, PRIMARY KEY (team_id, user_id));
                CREATE INDEX IF NOT EXISTS idx_members_user ON members(user_id);
                CREATE TABLE IF NOT EXISTS invites (
                    code_hash TEXT PRIMARY KEY, team_id TEXT NOT NULL, role TEXT NOT NULL, created_by TEXT NOT NULL,
                    expires REAL NOT NULL, used_by TEXT);
                CREATE TABLE IF NOT EXISTS audit (
                    team_id TEXT NOT NULL, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                    detail TEXT DEFAULT '');
                CREATE INDEX IF NOT EXISTS idx_audit_team ON audit(team_id, ts);
            """)

    @contextlib.contextmanager
    def _conn(self):
        """A registry connection that commits on success, rolls back on error and is always closed."""
        conn = sqlite3.connect(self.data_dir / "teams.db", timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _db_path(self, team_id: str) -> Path:
        return self.data_dir / f"team_{_hash('team:' + team_id)}.db"

    def backend(self, team_id: str):
        from cognicore.memory import SQLiteMemoryBackend

        return SQLiteMemoryBackend(str(self._db_path(team_id)), provider=self.provider)

    def _audit(self, conn, team_id: str, actor: str, action: str, detail: str = "") -> None:
        conn.execute("INSERT INTO audit (team_id, ts, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
                     (team_id, time.time(), actor, action, detail[:300]))

    # --- membership ------------------------------------------------------------------------

    def teams_for(self, user: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT t.team_id, t.name, m.role FROM members m JOIN teams t USING (team_id) "
                                "WHERE m.user_id = ? ORDER BY t.name", (user,)).fetchall()
        return [dict(r) for r in rows]

    def resolve(self, user: str, team: str) -> Dict[str, Any]:
        """The user's team named or identified by *team*; TeamError if they are not in one."""
        team = (team or "").strip()
        if not team:
            raise TeamError("Name a team.")
        mine = self.teams_for(user)
        exact = [t for t in mine if t["team_id"] == team]
        named = [t for t in mine if t["name"].lower() == team.lower()]
        if exact:
            return exact[0]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            raise TeamError(f"You are in several teams named {team!r}; use the team id: "
                            + ", ".join(t["team_id"] for t in named))
        raise TeamError(NOT_FOUND)

    def _require(self, user: str, team: str, *roles: str) -> Dict[str, Any]:
        t = self.resolve(user, team)
        if roles and t["role"] not in roles:
            raise TeamError(f"This needs the {' or '.join(roles)} role in {t['name']}; you are {t['role']}.")
        return t

    def create(self, user: str, name: str, display: str = "") -> Dict[str, Any]:
        name = " ".join((name or "").split())[:NAME_CHARS]
        if not name:
            raise TeamError("Give the team a name.")
        with self._conn() as conn:
            owned = conn.execute("SELECT COUNT(*) FROM teams WHERE created_by = ?", (user,)).fetchone()[0]
            if owned >= MAX_OWNED_TEAMS:
                raise TeamError(f"You already created {MAX_OWNED_TEAMS} teams.")
            team_id = secrets.token_hex(6)
            now = time.time()
            conn.execute("INSERT INTO teams (team_id, name, created_by, created) VALUES (?, ?, ?, ?)",
                         (team_id, name, user, now))
            conn.execute("INSERT INTO members (team_id, user_id, role, name, added) VALUES (?, ?, 'owner', ?, ?)",
                         (team_id, user, display[:NAME_CHARS], now))
            self._audit(conn, team_id, user, "create", name)
        return {"team_id": team_id, "name": name, "role": "owner"}

    def invite(self, user: str, team: str, role: str = "editor") -> Tuple[str, Dict[str, Any]]:
        """A single-use invite code for *team*, valid INVITE_TTL seconds. Owners only."""
        role = (role or "editor").strip().lower()
        if role not in ROLES:
            raise TeamError("Role must be owner, editor or viewer.")
        t = self._require(user, team, "owner")
        code = "cct-" + secrets.token_urlsafe(12)
        now = time.time()
        with self._conn() as conn:
            conn.execute("DELETE FROM invites WHERE expires < ? AND used_by IS NULL", (now,))
            open_invites = conn.execute("SELECT COUNT(*) FROM invites WHERE team_id = ? AND used_by IS NULL",
                                        (t["team_id"],)).fetchone()[0]
            if open_invites >= MAX_OPEN_INVITES:
                raise TeamError(f"{t['name']} already has {MAX_OPEN_INVITES} unused invites.")
            conn.execute("INSERT INTO invites (code_hash, team_id, role, created_by, expires) VALUES (?, ?, ?, ?, ?)",
                         (_hash(code), t["team_id"], role, user, now + INVITE_TTL))
            self._audit(conn, t["team_id"], user, "invite", role)
        return code, t

    def join(self, user: str, code: str, display: str = "") -> Dict[str, Any]:
        code = (code or "").strip()
        now = time.time()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM invites WHERE code_hash = ?", (_hash(code),)).fetchone()
            if not row or row["used_by"] or row["expires"] < now:
                raise TeamError("That invite code is not valid: it may be mistyped, used or expired.")
            team = conn.execute("SELECT * FROM teams WHERE team_id = ?", (row["team_id"],)).fetchone()
            if not team:
                raise TeamError("That invite code is not valid: it may be mistyped, used or expired.")
            existing = conn.execute("SELECT role FROM members WHERE team_id = ? AND user_id = ?",
                                    (team["team_id"], user)).fetchone()
            if existing:
                return {"team_id": team["team_id"], "name": team["name"], "role": existing["role"]}
            count = conn.execute("SELECT COUNT(*) FROM members WHERE team_id = ?", (team["team_id"],)).fetchone()[0]
            if count >= MAX_MEMBERS:
                raise TeamError(f"{team['name']} already has {MAX_MEMBERS} members.")
            claimed = conn.execute("UPDATE invites SET used_by = ? WHERE code_hash = ? AND used_by IS NULL",
                                   (user, row["code_hash"])).rowcount
            if not claimed:
                raise TeamError("That invite code is not valid: it may be mistyped, used or expired.")
            conn.execute("INSERT INTO members (team_id, user_id, role, name, added) VALUES (?, ?, ?, ?, ?)",
                         (team["team_id"], user, row["role"], display[:NAME_CHARS], now))
            self._audit(conn, team["team_id"], user, "join", row["role"])
        return {"team_id": team["team_id"], "name": team["name"], "role": row["role"]}

    def members(self, user: str, team: str) -> List[Dict[str, Any]]:
        t = self._require(user, team)
        with self._conn() as conn:
            rows = conn.execute("SELECT user_id, role, name, added FROM members WHERE team_id = ? "
                                "ORDER BY added", (t["team_id"],)).fetchall()
        return [dict(r) for r in rows]

    def _owners(self, conn, team_id: str) -> int:
        return conn.execute("SELECT COUNT(*) FROM members WHERE team_id = ? AND role = 'owner'",
                            (team_id,)).fetchone()[0]

    def set_role(self, user: str, team: str, member: str, role: str) -> str:
        role = (role or "").strip().lower()
        if role not in ROLES:
            raise TeamError("Role must be owner, editor or viewer.")
        t = self._require(user, team, "owner")
        with self._conn() as conn:
            row = conn.execute("SELECT role FROM members WHERE team_id = ? AND user_id = ?",
                               (t["team_id"], member)).fetchone()
            if not row:
                raise TeamError(f"{member} is not a member of {t['name']}.")
            if row["role"] == "owner" and role != "owner" and self._owners(conn, t["team_id"]) == 1:
                raise TeamError("A team needs at least one owner; make someone else owner first.")
            conn.execute("UPDATE members SET role = ? WHERE team_id = ? AND user_id = ?", (role, t["team_id"], member))
            self._audit(conn, t["team_id"], user, "set_role", f"{member} -> {role}")
        return role

    def remove(self, user: str, team: str, member: str = "") -> str:
        """Remove *member* (owners only), or leave the team when *member* is empty or the caller."""
        member = (member or "").strip() or user
        t = self._require(user, team) if member == user else self._require(user, team, "owner")
        with self._conn() as conn:
            row = conn.execute("SELECT role FROM members WHERE team_id = ? AND user_id = ?",
                               (t["team_id"], member)).fetchone()
            if not row:
                raise TeamError(f"{member} is not a member of {t['name']}.")
            if row["role"] == "owner" and self._owners(conn, t["team_id"]) == 1:
                raise TeamError("The last owner cannot leave; make someone else owner, or delete the team.")
            conn.execute("DELETE FROM members WHERE team_id = ? AND user_id = ?", (t["team_id"], member))
            self._audit(conn, t["team_id"], user, "leave" if member == user else "remove", member)
        return t["name"]

    def delete(self, user: str, team: str) -> str:
        """Delete the team, its members, invites, audit log and shared memory. Owners only."""
        t = self._require(user, team, "owner")
        with self._conn() as conn:
            for table in ("members", "invites", "audit", "teams"):
                conn.execute(f"DELETE FROM {table} WHERE team_id = ?", (t["team_id"],))  # nosec B608 - fixed names
        _erase(self._db_path(t["team_id"]))
        return t["name"]

    def audit_log(self, user: str, team: str, limit: int = 50) -> List[Dict[str, Any]]:
        t = self._require(user, team)
        with self._conn() as conn:
            rows = conn.execute("SELECT ts, actor, action, detail FROM audit WHERE team_id = ? "
                                "ORDER BY ts DESC LIMIT ?", (t["team_id"], max(1, min(limit, 500)))).fetchall()
        return [dict(r) for r in rows]

    # --- shared memory ---------------------------------------------------------------------

    def share(self, user: str, team: str, facts: List[Tuple[str, str]]) -> Tuple[Dict[str, Any], List[str], int]:
        """Store (text, category) facts in the team's memory. Editors and owners.

        Returns (team, entry ids, secrets redacted)."""
        from cognicore.memory import MemoryEntry, MemoryScope
        from cognicore.memory.redact import redact_secrets

        t = self._require(user, team, "owner", "editor")
        backend = self.backend(t["team_id"])
        ids, redacted = [], 0
        for text, category in facts:
            clean, n = redact_secrets(text)
            redacted += n
            ids.append(str(backend.store(MemoryEntry(
                text=clean, category=category, scope=MemoryScope.GLOBAL, memory_type="semantic",
                metadata={"shared_by": user, "team_id": t["team_id"]}, source_agent=user))))
        with self._conn() as conn:
            self._audit(conn, t["team_id"], user, "share", f"{len(ids)} memories: ids {','.join(ids)}")
        return t, ids, redacted

    def search(self, user: str, query: str, top_k: int = 5, category: Optional[str] = None):
        """[(team, SearchResult)] from every team the user is in, best first."""
        found = []
        for t in self.teams_for(user):
            try:
                results = self.backend(t["team_id"]).search(query=query, top_k=top_k, category=category)
            except Exception:
                continue
            found += [(t, r) for r in results]
        found.sort(key=lambda tr: tr[1].score, reverse=True)
        return found[:top_k]

    def forget(self, user: str, team_id: str, entry_id: str) -> bool:
        """Delete a team memory: owners any, editors only those they shared."""
        t = self._require(user, team_id, "owner", "editor")
        backend = self.backend(t["team_id"])
        entry = backend.get_by_id(str(entry_id))
        if entry is None:
            return False
        if t["role"] != "owner" and (entry.metadata or {}).get("shared_by") != user:
            raise TeamError("Editors can delete only the memories they shared.")
        ok = backend.delete(str(entry_id))
        if ok:
            with self._conn() as conn:
                self._audit(conn, t["team_id"], user, "forget", f"id {entry_id}")
        return ok

    def display_names(self, team_id: str) -> Dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT user_id, name FROM members WHERE team_id = ?", (team_id,)).fetchall()
        return {r["user_id"]: (r["name"] or r["user_id"]) for r in rows}


def _erase(db: Path) -> None:
    """Delete a team's memory database. Where an open handle prevents deleting the file (Windows),
    empty every table instead, so the memories are gone either way."""
    import gc

    gc.collect()  # memory backends close their connections when collected
    files = [Path(str(db) + s) for s in ("", "-wal", "-shm", "-journal")]
    try:
        for f in files:
            f.unlink(missing_ok=True)
        return
    except PermissionError:
        pass
    with sqlite3.connect(db) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                              "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'memory_fts%'")]
        for table in tables:  # memory_entries' delete trigger empties its search index too
            conn.execute(f'DELETE FROM "{table}"')  # nosec B608 - names from sqlite_master
    with sqlite3.connect(db) as conn:
        conn.execute("VACUUM")


def blend_scores(query: str, items: List[Any], texts: List[str], scores: List[float]) -> List[float]:
    """Scores for results that came from different memories (personal and teams), made comparable.

    Each memory scores its own results on its own scale, so their raw scores cannot be ranked
    against each other. This scores all of them with one BM25 over just these texts and averages
    that with each result's own (0..1) score."""
    import math
    import re
    from collections import Counter

    def words(text):
        return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1]

    docs = [words(t) for t in texts]
    query_words = set(words(query))
    n = len(docs) or 1
    df = Counter(w for d in docs for w in set(d))
    avg = (sum(len(d) for d in docs) / n) or 1.0
    bm25 = []
    for d in docs:
        tf = Counter(d)
        bm25.append(sum(math.log((n - df[w] + 0.5) / (df[w] + 0.5) + 1) * tf[w] * 2.5
                        / (tf[w] + 1.5 * (0.25 + 0.75 * len(d) / avg)) for w in query_words if w in tf))
    top = max(bm25, default=0.0) or 1.0
    return [0.5 * max(0.0, min(1.0, s)) + 0.5 * b / top for s, b in zip(scores, bm25)]


def parse_team_entry_id(entry_id: str) -> Optional[Tuple[str, str]]:
    """("team id", "entry id") for an id like "team:<team_id>:<entry_id>", else None."""
    parts = str(entry_id).split(":")
    if len(parts) == 3 and parts[0] == "team" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None

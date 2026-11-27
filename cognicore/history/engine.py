"""Store historical provenance and index it with the existing SQLite backend."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
import zipfile
from collections import deque
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from cognicore.experience.schema import EvidenceRecord, StructuredExperience
from cognicore.experience.verification import VerificationGate
from cognicore.memory.base import MemoryEntry, MemoryScope, MemoryState, MemoryType
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend

from .chatgpt import MAX_ARCHIVE, ChatGPTAdapter, clean, digest, event_time
from .extraction import extract
from .schema import SCHEMA


class Classification(str, Enum):
    UNVERIFIED = "HISTORICAL_UNVERIFIED"
    ATTACHED = "HISTORICAL_EVIDENCE_ATTACHED"
    REVERIFIED = "REVERIFIED"
    CONFLICTED = "CONFLICTED"
    SUPERSEDED = "SUPERSEDED"


class _HistoryConnection(sqlite3.Connection):
    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


class HistoryBackend(SQLiteMemoryBackend):
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30, factory=_HistoryConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        return conn


class HistoryEngine:
    """A private archive indexed as episodic and structured CogniCore memory."""

    def __init__(self, db_path, *, adapter=None, extractor=extract, embedding_provider=None):
        path = Path(db_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.backend = HistoryBackend(str(path), provider=embedding_provider, publish_events=False)
        self.adapter = adapter or ChatGPTAdapter()
        self.extractor = extractor
        with closing(self.backend._get_conn()) as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _transaction(self, *, write=False):
        with self.backend._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn

    @staticmethod
    def _audit(conn, action, target):
        conn.execute("INSERT INTO history_audit (at, action, target) VALUES (?, ?, ?)",
                     (time.time(), action, target))

    def import_archive(self, path, *, consent=False, project="", redact=True):
        if consent is not True:
            raise PermissionError("Explicit consent is required to import conversation history.")
        with Path(path).open("rb") as source:
            return self.import_stream(source, consent=consent, project=project, redact=redact)

    def import_stream(self, source, *, consent=False, project="", redact=True):
        """Import a seekable binary stream. Each committed conversation is a checkpoint."""
        if consent is not True:
            raise PermissionError("Explicit consent is required to import conversation history.")
        project = clean(project.strip()) if redact else project.strip()
        source.seek(0, os.SEEK_END)
        if source.tell() > MAX_ARCHIVE:
            raise ValueError("Archive exceeds the size limit.")
        source.seek(0)
        fingerprint = hashlib.sha256()
        while chunk := source.read(1024 * 1024):
            fingerprint.update(chunk)
        source.seek(0)
        with self._transaction(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM history_batches WHERE fingerprint = ?", (fingerprint.hexdigest(),)
            ).fetchone()
            if row:
                if row["project"] != project or bool(row["redact"]) != redact or row["platform"] != self.adapter.platform:
                    raise ValueError("Resume with the original project, redaction, and platform settings.")
                if row["status"] == "complete":
                    return {**self._batch(conn, row["id"]), "duplicate": True}
                batch, checkpoint, imported_at = row["id"], row["processed"], row["imported_at"]
                conn.execute("UPDATE history_batches SET status='running', error=NULL WHERE id=?", (batch,))
                self._audit(conn, "import_resumed", batch)
            else:
                batch, checkpoint, imported_at = uuid.uuid4().hex, 0, time.time()
                conn.execute(
                    """INSERT INTO history_batches (id, fingerprint, platform, imported_at, project, redact, status)
                       VALUES (?, ?, ?, ?, ?, ?, 'running')""",
                    (batch, fingerprint.hexdigest(), self.adapter.platform, imported_at, project, int(redact)),
                )
                self._audit(conn, "import_started", batch)
        try:
            for position, item in enumerate(self.adapter.conversations(source), start=1):
                if position <= checkpoint:
                    continue
                with self._transaction(write=True) as conn:
                    current = conn.execute("SELECT processed FROM history_batches WHERE id=?", (batch,)).fetchone()
                    if current is None:
                        raise ValueError("The import batch was deleted.")
                    if position <= current["processed"]:
                        continue
                    if item is not None:
                        self._store_conversation(conn, batch, project, imported_at, redact, *item)
                    conn.execute(
                        "UPDATE history_batches SET processed=?, skipped=skipped+? WHERE id=?",
                        (position, int(item is None), batch),
                    )
        except (OSError, ValueError, RuntimeError, RecursionError, zipfile.BadZipFile, sqlite3.Error) as exc:
            with self._transaction(write=True) as conn:
                if conn.execute(
                    "UPDATE history_batches SET status='interrupted', error=? WHERE id=? AND status='running'",
                    (type(exc).__name__, batch),
                ).rowcount:
                    self._audit(conn, "import_interrupted", batch)
            raise ValueError(f"Import interrupted for batch {batch} ({type(exc).__name__}).") from None
        with self._transaction(write=True) as conn:
            if conn.execute(
                "UPDATE history_batches SET status='complete', error=NULL WHERE id=? AND status<>'complete'", (batch,)
            ).rowcount:
                self._audit(conn, "import_completed", batch)
            return self._batch(conn, batch)

    def _index(self, conn, text, category, project, timestamp, ref, *, experience=False):
        embedding = None
        if self.backend.provider and text:
            embedding = self.backend._embedding_json(self.backend.provider.embed(text))
        entry = MemoryEntry(
            text=text, category=category, scope=MemoryScope.PROJECT if project else MemoryScope.USER,
            scope_id=project.casefold(), timestamp=timestamp,
            metadata={"history_ref": ref, "history_kind": "claim" if experience else "message"},
            state=MemoryState.UNVERIFIED.value, confidence=0.0,
            memory_type=MemoryType.EXPERIENCE.value if experience else MemoryType.EPISODIC.value,
            source_component="history",
        )
        return self.backend._insert(conn, entry, embedding, preserve_timestamp=True)

    def _store_conversation(self, conn, batch, project, imported_at, redact, conversation, messages):
        platform = self.adapter.platform
        source_key = conversation["original_conversation_id"] or digest(
            [conversation["content_hash"], [message["content_hash"] for message in messages]]
        )
        source_id = digest([platform, source_key])
        if conn.execute("SELECT 1 FROM history_deleted_sources WHERE batch_id=? AND source_id=?",
                        (batch, source_id)).fetchone():
            return
        provenance = {"source_platform": platform, "import_batch_id": batch, "imported_at": imported_at}
        if redact:
            conversation = clean(conversation)
        conn.execute(
            "INSERT OR IGNORE INTO history_sources VALUES (?, ?, ?, ?, ?)",
            (source_id, platform, conversation["original_conversation_id"], project,
             _provenance_json({**conversation, **provenance})),
        )
        source = conn.execute("SELECT project, payload FROM history_sources WHERE id=?", (source_id,)).fetchone()
        project = source["project"]
        title = json.loads(source["payload"])["title"]
        conn.execute("INSERT OR IGNORE INTO history_batch_sources VALUES (?, ?)", (batch, source_id))
        for original in messages:
            message_id = digest([source_id, original["content_hash"]])
            if not conn.execute("SELECT 1 FROM history_messages WHERE id=?", (message_id,)).fetchone():
                message = clean(original) if redact else original
                message = {**message, **provenance}
                timestamp = message["event_time"]
                entry_id = self._index(conn, f"{title}\n{message['text']}", "message", project, timestamp, message_id)
                conn.execute("INSERT INTO history_messages VALUES (?, ?, ?, ?, ?)",
                             (message_id, source_id, entry_id, timestamp, _provenance_json(message)))
                for claim in self.extractor(message):
                    self._store_claim(conn, source_id, message_id, title, project, timestamp, claim)
            conn.execute("INSERT OR IGNORE INTO history_batch_messages VALUES (?, ?)", (batch, message_id))

    def _store_claim(self, conn, source_id, message_id, title, project, timestamp, claim):
        claim_id = digest([message_id, claim])
        if conn.execute("SELECT 1 FROM history_claims WHERE id=?", (claim_id,)).fetchone():
            return
        category, subject = claim["kind"], claim["subject"]
        group = f"project:{project.casefold()}" if project else f"source:{source_id}"
        entry_id = self._index(conn, f"{title}\n{claim['text']}", category, project, timestamp, claim_id,
                               experience=True)
        conn.execute("INSERT INTO history_claims VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (claim_id, message_id, entry_id, category, subject, group, json.dumps(claim)))
        if subject and category in {"failure", "success"}:
            opposite = "failure" if category == "success" else "success"
            previous = conn.execute(
                "SELECT id, payload FROM history_claims WHERE group_key=? AND subject=? AND kind=?",
                (group, subject, opposite),
            )
            for other in previous:
                left, right = sorted((claim_id, other["id"]))
                environment = claim["fields"]["environment"]
                other_environment = json.loads(other["payload"])["fields"]["environment"]
                kind = "conflict" if (environment or "").casefold() == (other_environment or "").casefold() else "related_approach"
                conn.execute("INSERT OR IGNORE INTO history_relations VALUES (?, ?, ?)", (left, right, kind))

    def _batch(self, conn, batch):
        alias = conn.execute("SELECT batch_id FROM history_sync_aliases WHERE alias=?", (batch,)).fetchone()
        if alias:
            batch = alias["batch_id"]
        row = conn.execute(
            """SELECT id, platform, imported_at, project, redact, status, processed, skipped, error,
                 (SELECT COUNT(*) FROM history_batch_sources WHERE batch_id=b.id) AS conversations,
                 (SELECT COUNT(*) FROM history_batch_messages WHERE batch_id=b.id) AS messages,
                 (SELECT COUNT(*) FROM history_batch_messages bm JOIN history_claims c ON c.message_id=bm.message_id
                    WHERE bm.batch_id=b.id) AS experiences
               FROM history_batches b WHERE id=?""", (batch,),
        ).fetchone()
        if not row:
            raise KeyError("Unknown history batch.")
        return dict(row)

    def status(self, batch=""):
        with self._transaction() as conn:
            if batch:
                return self._batch(conn, batch)
            return [self._batch(conn, row["id"]) for row in conn.execute(
                "SELECT id FROM history_batches ORDER BY imported_at DESC"
            )]

    def audit(self, limit=50):
        with self._transaction() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT at, action, target FROM history_audit ORDER BY id DESC LIMIT ?", (_limit(limit),)
            )]

    @staticmethod
    def _source_id(conn, ref):
        row = conn.execute("SELECT id FROM history_sources WHERE id=?", (ref,)).fetchone()
        if row:
            return row["id"]
        rows = conn.execute("SELECT id FROM history_sources WHERE original_id=? LIMIT 2", (ref,)).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        if rows:
            raise ValueError("The original ID is ambiguous. Use the source reference.")
        raise KeyError("Unknown historical conversation.")

    def _message(self, conn, ref):
        row = conn.execute(
            """SELECT m.payload, m.source_id, s.original_id, s.project, json_extract(s.payload, '$.title') AS title
               FROM history_messages m JOIN history_sources s ON s.id=m.source_id WHERE m.id=?""", (ref,)
        ).fetchone()
        if not row:
            raise KeyError("Unknown historical message.")
        return {
            **_inspect_payload(row["payload"]), "message_ref": ref, "conversation_ref": row["source_id"],
            "original_conversation_id": row["original_id"], "project": row["project"], "title": row["title"],
            "evidence_classification": Classification.UNVERIFIED.value, "data_kind": "source_message",
        }

    def inspect(self, conversation, *, message="", limit=50, offset=0):
        with self._transaction() as conn:
            ref = self._source_id(conn, conversation)
            row = conn.execute("SELECT project, payload FROM history_sources WHERE id=?", (ref,)).fetchone()
            if message:
                item = self._message(conn, message)
                if item["conversation_ref"] != ref:
                    raise KeyError("Unknown message in historical conversation.")
                messages = [item]
            else:
                messages = [self._message(conn, item["id"]) for item in conn.execute(
                    """SELECT id FROM history_messages WHERE source_id=?
                       ORDER BY event_time IS NULL, event_time, entry_id LIMIT ? OFFSET ?""",
                    (ref, _limit(limit), max(0, offset)),
                )]
            return {**_inspect_payload(row["payload"]), "conversation_ref": ref, "project": row["project"],
                    "messages": messages}

    def _claim(self, conn, ref):
        row = conn.execute("SELECT message_id, payload FROM history_claims WHERE id=?", (ref,)).fetchone()
        if not row:
            raise KeyError("Unknown historical experience.")
        source = self._message(conn, row["message_id"])
        relations = [dict(item) for item in conn.execute(
            "SELECT origin, target, kind FROM history_relations WHERE origin=? OR target=? ORDER BY kind, origin, target",
            (ref, ref),
        )]
        evidence = [{"classification": item["classification"], **json.loads(item["payload"])}
                    for item in conn.execute(
                        "SELECT classification, payload FROM history_evidence WHERE claim_id=? ORDER BY rowid", (ref,)
                    )]
        classification = Classification.UNVERIFIED.value
        if evidence:
            classification = Classification.ATTACHED.value
        if any(item["classification"] == Classification.REVERIFIED.value for item in evidence):
            classification = Classification.REVERIFIED.value
        if any(link["kind"] == "conflict" for link in relations):
            classification = Classification.CONFLICTED.value
        if any(link["kind"] == "supersedes" and link["target"] == ref for link in relations):
            classification = Classification.SUPERSEDED.value
        return {
            **json.loads(row["payload"]), "experience_ref": ref, "data_kind": "extracted_claim",
            "project": source["project"], "source_platform": source["source_platform"],
            "original_conversation_id": source["original_conversation_id"],
            "title": source["title"],
            "conversation_ref": source["conversation_ref"], "event_time": source["event_time"],
            "original_created_at": source["original_created_at"], "message_refs": [row["message_id"]],
            "source_content_intact": source["stored_content_intact"],
            "evidence_classification": classification, "evidence": evidence, "relations": relations,
        }

    def experience(self, ref):
        with self._transaction() as conn:
            return self._claim(conn, ref)

    def search(self, query, *, project="", kind="", limit=10):
        return self._search(query, project, kind, limit)

    def _search(self, query, project, kind, limit, *, claims_only=False):
        if not query.strip() or len(query) > 2000:
            raise ValueError("Use a non-empty historical query of at most 2000 characters.")
        scope = project.strip().casefold() or None
        metadata = {"history_kind": "claim"} if claims_only else None
        limit = _limit(limit)
        hits = self.backend.search(query, top_k=limit, category=kind or None, scope_id=scope,
                                   metadata_filters=metadata)
        with self._transaction() as conn:
            if claims_only and not hits:
                # Strict FTS matches can all be source messages. Try partial matches among claims.
                hits = self.backend._fallback_search(conn, query, limit, None, None, scope, metadata)
            results = []
            for hit in hits:
                if hit.score <= 0:
                    continue
                ref = hit.entry.metadata["history_ref"]
                try:
                    if hit.entry.category == "message":
                        result = _excerpt(self._message(conn, ref))
                    else:
                        result = self._claim(conn, ref)
                except KeyError:
                    continue  # A concurrent deletion removed the record.
                results.append({**result, "relevance": hit.score})
            return results

    def timeline(self, *, project="", kind="", query="", earliest=False, limit=50):
        """Fetch events chronologically, with unknown event dates last."""
        records = "history_messages m" if kind == "message" else (
            "history_claims c JOIN history_messages m ON m.id=c.message_id"
        )
        ref = "m.id" if kind == "message" else "c.id"
        entry = "m.entry_id" if kind == "message" else "c.entry_id"
        # Table and column expressions are selected from fixed literals above.
        sql = f"""SELECT {ref} AS ref FROM {records}
                  JOIN history_sources s ON s.id=m.source_id JOIN memory_entries e ON e.entry_id={entry}
                  WHERE 1=1"""  # nosec B608
        params = []
        if project.strip():
            sql += " AND e.scope_id=?"
            params.append(project.strip().casefold())
        if kind and kind != "message":
            sql += " AND c.kind=?"
            params.append(kind)
        if query:
            tokens = re.findall(r"\w+", query)
            if not tokens or len(query) > 2000:
                raise ValueError("Use a historical timeline query of at most 2000 characters with searchable words.")
            sql += " AND e.entry_id IN (SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?)"
            params.append(" ".join(f'"{word}"*' for word in tokens))
        if earliest:
            sql += " AND m.event_time IS NOT NULL"
        sql += " ORDER BY m.event_time IS NULL, m.event_time, e.entry_id LIMIT ?"
        params.append(1 if earliest else _limit(limit))
        with self._transaction() as conn:
            if kind == "message":
                return [_excerpt(self._message(conn, row["ref"])) for row in conn.execute(sql, params)]
            return [self._claim(conn, row["ref"]) for row in conn.execute(sql, params)]

    def reconstruct(self, query, *, project="", limit=20):
        """Combine cross-source retrieval with explicit links and chronological context."""
        limit = _limit(limit)
        scope = project.strip().casefold()
        matches = self._search(query, project, "", limit, claims_only=True)
        seeds = matches[:max(1, limit // 2)]
        claims = {hit["experience_ref"]: hit for hit in seeds}
        pending = deque(seeds)
        visited = set(claims)
        with self._transaction() as conn:
            while pending and len(claims) < limit:
                hit = pending.popleft()
                for link in hit["relations"]:
                    for ref in (link["origin"], link["target"]):
                        if len(claims) >= limit:
                            break
                        if ref in visited:
                            continue
                        visited.add(ref)
                        try:
                            related = self._claim(conn, ref)
                        except KeyError:
                            continue
                        if not scope or related["project"].casefold() == scope:
                            claims[ref] = related
                            pending.append(related)
        for hit in matches:
            if len(claims) >= limit:
                break
            claims.setdefault(hit["experience_ref"], hit)
        return sorted(claims.values(), key=lambda hit: (hit["event_time"] is None, hit["event_time"] or 0,
                                                        hit["experience_ref"]))

    def relate(self, origin, target, kind):
        """Record a reviewed relationship between two historical experiences."""
        if kind not in {"related_approach", "conflict", "supersedes"} or origin == target:
            raise ValueError("Use two different experiences and a supported relation.")
        if kind in {"conflict", "related_approach"}:
            origin, target = sorted((origin, target))
        with self._transaction(write=True) as conn:
            for ref in (origin, target):
                self._claim(conn, ref)
            if kind == "supersedes":
                reachable = conn.execute(
                    """WITH RECURSIVE linked(id) AS (
                           SELECT ? UNION SELECT r.target FROM linked JOIN history_relations r ON r.origin=linked.id
                           WHERE r.kind='supersedes'
                       ) SELECT 1 FROM linked WHERE id=?""", (target, origin)
                ).fetchone()
                if reachable:
                    raise ValueError("Supersession would form a cycle.")
            conn.execute("INSERT OR IGNORE INTO history_relations VALUES (?, ?, ?)", (origin, target, kind))
            self._audit(conn, f"link_{kind}", origin)

    def attach_evidence(self, ref, *, command, exit_code, output, timestamp, source, reverify=False, commit=""):
        """Record a trusted operator's execution receipt. This API never runs the command."""
        observed = event_time(timestamp)
        if observed is None or not source.strip() or not command.strip() or type(exit_code) is not int:
            raise ValueError("Supply a dated execution receipt with a command, integer exit code, and source.")
        if not isinstance(output, bytes):
            raise ValueError("Supply the original execution output as bytes.")
        with self._transaction(write=True) as conn:
            claim = self._claim(conn, ref)
            receipt = EvidenceRecord(
                command=clean(command), exit_code=exit_code, stdout_hash=hashlib.sha256(output).hexdigest(),
                timestamp=datetime.fromtimestamp(observed, timezone.utc).isoformat(), commit=clean(commit),
            )
            classification = Classification.ATTACHED.value
            if reverify:
                imported_at = self._message(conn, claim["message_refs"][0])["imported_at"]
                if observed <= imported_at or observed > time.time():
                    raise ValueError("Reverification requires a new execution dated after import.")
                result = VerificationGate().verify(StructuredExperience(task=claim["text"]), [receipt])
                if not result.passed:
                    raise ValueError("Reverification requires a successful execution.")
                classification = Classification.REVERIFIED.value
            payload = {**receipt.to_dict(), "source": clean(source), "provenance": "local_operator_attested"}
            conn.execute("INSERT OR IGNORE INTO history_evidence VALUES (?, ?, ?, ?)",
                         (ref, digest([classification, payload]), classification, json.dumps(payload)))
            self._audit(conn, "execution_evidence_attached", ref)
            return self._claim(conn, ref)

    def delete(self, *, batch="", conversation=""):
        if bool(batch) == bool(conversation):
            raise ValueError("Choose exactly one batch or conversation.")
        with self._transaction(write=True) as conn:
            if batch:
                batch = self._batch(conn, batch)["id"]
                conn.execute("""INSERT OR IGNORE INTO history_sync_tombstones
                                SELECT 'batch', fingerprint FROM history_batches WHERE id=?""", (batch,))
                conn.execute("DELETE FROM history_batches WHERE id=?", (batch,))
                conn.execute("""DELETE FROM history_messages WHERE NOT EXISTS
                                (SELECT 1 FROM history_batch_messages b WHERE b.message_id=history_messages.id)""")
                conn.execute("""DELETE FROM history_sources WHERE NOT EXISTS
                                (SELECT 1 FROM history_batch_sources b WHERE b.source_id=history_sources.id)""")
                self._audit(conn, "batch_deleted", batch)
            else:
                ref = self._source_id(conn, conversation)
                conn.execute("INSERT OR IGNORE INTO history_sync_tombstones VALUES ('source', ?)", (ref,))
                conn.execute("""INSERT OR IGNORE INTO history_deleted_sources
                                SELECT id, ? FROM history_batches WHERE status IN ('running', 'interrupted')""", (ref,))
                conn.execute("DELETE FROM history_sources WHERE id=?", (ref,))
                self._audit(conn, "source_deleted", ref)


def _limit(value):
    return max(1, min(value, 200))


def _provenance_json(payload):
    return json.dumps({**payload, "stored_content_hash": digest(payload)})


def _inspect_payload(serialized):
    payload = json.loads(serialized)
    stored = payload.pop("stored_content_hash", None)
    return {**payload, "stored_content_hash": stored,
            "stored_content_intact": stored == digest(payload) if stored else None}


def _excerpt(message):
    excerpt = {key: value for key, value in message.items() if key not in {"content", "attachments", "tool_interaction"}}
    excerpt["text"] = message["text"][:4000]
    return excerpt


def history_path():
    """Use the same local data directory convention as CogniCore's memory hooks."""
    root = Path(os.environ.get("COGNICORE_DATA_DIR") or Path.home() / ".cognicore" / "mcp").expanduser()
    return root / "history" / "history.db"


def open_history():
    return HistoryEngine(history_path())

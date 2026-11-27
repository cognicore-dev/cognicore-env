"""Logical history snapshots. SQLite page files and local account state stay local."""

import base64
import json
import math
import zlib

from .chatgpt import digest

MAX_COMPRESSED = 32 * 1024**2
MAX_DOCUMENT = 256 * 1024**2
MAX_WIRE = 4 * ((MAX_COMPRESSED + 2) // 3)
INDEXED_TEXT = {"history_sources": "title", "history_messages": "text", "history_claims": "text"}

# Columns and primary key lengths are fixed by the local schema, never by the payload.
TABLES = {
    "history_batches": ("id fingerprint platform imported_at project redact status processed skipped error", 1),
    "history_sources": ("id platform original_id project payload", 1),
    "history_messages": ("id source_id event_time payload", 1),
    "history_batch_sources": ("batch_id source_id", 2),
    "history_batch_messages": ("batch_id message_id", 2),
    "history_deleted_sources": ("batch_id source_id", 2),
    "history_claims": ("id message_id kind subject group_key payload", 1),
    "history_relations": ("origin target kind", 3),
    "history_evidence": ("claim_id id classification payload", 2),
    "history_sync_tombstones": ("kind ref", 2),
    "history_sync_aliases": ("alias batch_id", 1),
}


def _key(row, count):
    return json.dumps(row[:count], separators=(",", ":"))


def read(conn):
    result = {}
    for table, (columns, count) in TABLES.items():
        # All interpolated identifiers are fixed in TABLES.
        sql = f"SELECT {columns.replace(' ', ', ')} FROM {table}"  # nosec B608
        result[table] = {_key(row, count): row for row in map(list, conn.execute(sql))}
    return result


def manifest(snapshot):
    return {table: {key: digest(row) for key, row in rows.items()} for table, rows in snapshot.items()}


def encode(snapshot):
    payload = json.dumps({"version": 1, "history": snapshot}, ensure_ascii=True, separators=(",", ":")).encode()
    if len(payload) > MAX_DOCUMENT:
        raise ValueError("History exceeds the sync document size limit.")
    compressed = zlib.compress(payload)
    if len(compressed) > MAX_COMPRESSED:
        raise ValueError("History exceeds the compressed sync size limit.")
    return base64.b64encode(compressed).decode("ascii")


def decode(payload):
    if payload is None:
        return {table: {} for table in TABLES}
    if not isinstance(payload, str) or len(payload) > MAX_WIRE:
        raise ValueError("Invalid hosted history payload.")
    compressed = base64.b64decode(payload, validate=True)
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(compressed, MAX_DOCUMENT + 1)
        if len(raw) > MAX_DOCUMENT or not decoder.eof or decoder.unused_data:
            raise ValueError("Invalid hosted history document size.")
        doc = json.loads(raw)
    except (zlib.error, UnicodeError, RecursionError):
        raise ValueError("Invalid hosted history document.") from None
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise ValueError("Unsupported hosted history protocol.")
    snapshot = doc.get("history")
    if not isinstance(snapshot, dict) or set(snapshot) != set(TABLES):
        raise ValueError("Invalid hosted history tables.")
    for table, rows in snapshot.items():
        columns, count = TABLES[table]
        if not isinstance(rows, dict):
            raise ValueError("Invalid hosted history rows.")
        for key, row in rows.items():
            if (not isinstance(row, list) or len(row) != len(columns.split()) or
                    any(not isinstance(item, str) for item in row[:count]) or
                    any(item is not None and type(item) not in (str, int, float) for item in row) or
                    any(isinstance(item, float) and not math.isfinite(item) for item in row) or
                    _key(row, count) != key):
                raise ValueError("Invalid hosted history record.")
            if table in INDEXED_TEXT:
                data = json.loads(row[-1]) if isinstance(row[-1], str) else None
                if not isinstance(data, dict) or not isinstance(data.get(INDEXED_TEXT[table]), str):
                    raise ValueError("Invalid hosted history record content.")
    return snapshot


def merge(base, local, hosted):
    """Merge independent changes. A deletion of a known row wins over a stale update."""
    result = {}
    for table in TABLES:
        older = base.get(table, {})
        left, right = local[table], hosted[table]
        rows = {}
        for key in left.keys() | right.keys():
            a, b = left.get(key), right.get(key)
            old = older.get(key)
            if table == "history_sync_tombstones":
                row = a or b
            elif a == b or (b is not None and digest(b) == old):
                row = a
            elif a is not None and digest(a) == old:
                row = b
            elif old is not None and (a is None or b is None):
                row = None
            elif a is not None and b is not None and table == "history_batches":
                # Keep the highest durable import checkpoint. Complete wins an equal checkpoint.
                row = max((b, a), key=lambda item: (item[7], item[6] == "complete"))
            else:
                row = b or a
            if row is not None:
                rows[key] = row
        result[table] = rows
    _normalize(result, hosted)
    return result


def _normalize(snapshot, hosted):
    def rows(name):
        return snapshot[f"history_{name}"]

    def values(name):
        return list(rows(name).values())

    def replace(name, data):
        table = f"history_{name}"
        snapshot[table] = {_key(row, TABLES[table][1]): row for row in data}

    deleted = {(kind, ref) for kind, ref in values("sync_tombstones")}
    canonical = {}
    aliases = {}
    # Preserve batch references already published to this account.
    remote_batches = {row[0] for row in hosted["history_batches"].values()}
    for batch in sorted(values("batches"), key=lambda row: (row[0] not in remote_batches, row[0])):
        if ("batch", batch[1]) in deleted:
            continue
        preferred = canonical.setdefault(batch[1], batch)
        if preferred[0] != batch[0]:
            aliases[batch[0]] = preferred[0]
            if (batch[7], batch[6] == "complete") > (preferred[7], preferred[6] == "complete"):
                preferred = [*preferred[:6], *batch[6:]]
                canonical[batch[1]] = preferred
    replace("batches", canonical.values())
    batch_ids = {row[0] for row in canonical.values()}
    for original, target in values("sync_aliases"):
        aliases.setdefault(original, aliases.get(target, target))
    replace("sync_aliases", ([original, target] for original, target in aliases.items()
                             if target in batch_ids and original not in batch_ids))

    for name in ("batch_sources", "batch_messages", "deleted_sources"):
        replace(name, ([aliases.get(batch, batch), ref] for batch, ref in values(name)
                       if aliases.get(batch, batch) in batch_ids))

    source_members = {ref for _, ref in values("batch_sources") if ("source", ref) not in deleted}
    replace("sources", (row for row in values("sources") if row[0] in source_members))
    source_ids = {row[0] for row in values("sources")}
    replace("batch_sources", (row for row in values("batch_sources") if row[1] in source_ids))

    message_members = {ref for _, ref in values("batch_messages")}
    replace("messages", (row for row in values("messages") if row[0] in message_members and row[1] in source_ids))
    message_ids = {row[0] for row in values("messages")}
    replace("batch_messages", (row for row in values("batch_messages") if row[1] in message_ids))

    replace("claims", (row for row in values("claims") if row[1] in message_ids))
    claim_ids = {row[0] for row in values("claims")}
    replace("relations", (row for row in values("relations") if row[0] in claim_ids and row[1] in claim_ids))
    replace("evidence", (row for row in values("evidence") if row[0] in claim_ids))


def install(engine, conn, snapshot):
    """Rebuild derived local FTS rows without executing anything from the snapshot."""
    conn.execute("DELETE FROM history_batches")
    conn.execute("DELETE FROM history_sources")
    conn.execute("DELETE FROM history_sync_tombstones")
    source_index = {}
    message_sources = {}
    for table, (columns, _) in TABLES.items():
        rows = snapshot[table].values()
        if table == "history_sources":
            source_index = {row[0]: (json.loads(row[4])["title"], row[3]) for row in rows}
        if table in {"history_messages", "history_claims"}:
            experience = table == "history_claims"
            columns += " entry_id"
            indexed = []
            for row in rows:
                if experience:
                    title, project, timestamp = message_sources[row[1]]
                    category = row[2]
                else:
                    title, project = source_index[row[1]]
                    timestamp, category = row[2], "message"
                    message_sources[row[0]] = (title, project, timestamp)
                content = json.loads(row[-1])["text"]
                entry = engine._index(conn, f"{title}\n{content}", category, project, timestamp, row[0],
                                      experience=experience)
                indexed.append([*row, entry])
            rows = indexed
        placeholders = ", ".join("?" for _ in columns.split())
        sql = f"INSERT INTO {table} ({columns.replace(' ', ', ')}) VALUES ({placeholders})"  # nosec B608
        conn.executemany(sql, rows)

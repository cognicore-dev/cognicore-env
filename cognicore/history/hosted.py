"""Tenant-scoped storage for the history sync protocol."""

import os
from contextlib import closing

from cognicore.utils.sqlite import connect_sqlite


def exchange(path, owner, *, revision=None, payload=None):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    with closing(connect_sqlite(path)) as conn, conn:
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS history_snapshot (
                            id INTEGER PRIMARY KEY CHECK (id=1), revision INTEGER NOT NULL, payload TEXT NOT NULL
                        )""")
        row = conn.execute("SELECT revision, payload FROM history_snapshot WHERE id=1").fetchone()
        current = row[0] if row else 0
        if revision is None:
            return {"owner": owner, "revision": current, "payload": row[1] if row else None}
        if revision != current:
            return None
        conn.execute("""INSERT INTO history_snapshot VALUES (1, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, payload=excluded.payload""",
                     (current + 1, payload))
        return {"owner": owner, "revision": current + 1}

"""Opt-in synchronization through the existing CogniCore OAuth account."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from urllib.parse import urlsplit

from cognicore import sync as sessions

from . import replica
from .engine import history_path, open_history


def _configuration(engine):
    with engine._transaction() as conn:
        return conn.execute("SELECT * FROM history_sync_config WHERE id=1").fetchone()


def _connection():
    if not sessions.enabled():
        raise sessions.SyncError("Sign in with cognicore login and make sure sync is enabled.")
    server = sessions.load_state().get("server", "").rstrip("/")
    parsed = urlsplit(server)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
        raise sessions.SyncError("Historical sync requires HTTPS or a local development server.")
    token = sessions.access_token()
    if sessions.load_state().get("server", "").rstrip("/") != server:
        raise sessions.SyncError("The signed-in server changed during synchronization.")
    return server, token


def enable(engine, *, consent=False):
    if consent is not True:
        raise PermissionError("Pass --consent to sync the entire stored archive to the signed-in account.")
    server, token = _connection()
    remote = sessions._request("GET", f"{server}/sync/history", token=token, timeout=45)
    owner = remote.get("owner")
    if not isinstance(owner, str) or not owner:
        raise sessions.SyncError("The signed-in server did not identify the history account.")
    with engine._transaction(write=True) as conn:
        config = conn.execute("SELECT server, owner FROM history_sync_config WHERE id=1").fetchone()
        if config and config["owner"] and (config["server"], config["owner"]) != (server, owner):
            raise sessions.SyncError("This archive belongs to another sync account. Use a separate data directory.")
        conn.execute("""INSERT INTO history_sync_config (id, server, owner, enabled) VALUES (1, ?, ?, 1)
                        ON CONFLICT(id) DO UPDATE SET enabled=1, server=excluded.server, owner=excluded.owner""",
                     (server, owner))
        engine._audit(conn, "history_sync_enabled", owner)
    return synchronize(engine)


def disable(engine):
    with engine._transaction(write=True) as conn:
        conn.execute("""INSERT INTO history_sync_config (id, server, owner, enabled) VALUES (1, '', '', 0)
                        ON CONFLICT(id) DO UPDATE SET enabled=0""")
        engine._audit(conn, "history_sync_disabled", "")
    return {"enabled": False}


def status(engine):
    config = _configuration(engine)
    return {"enabled": bool(config and config["enabled"]),
            "server": config["server"] if config else None,
            "last_sync": config["last_sync"] if config else None}


def _restore_new_device(engine, conn, server, remote):
    """Join an opted-in account only when the device has no local history to upload."""
    owner = remote.get("owner")
    payload = remote.get("payload")
    if not isinstance(owner, str) or not owner or payload is None or any(replica.read(conn).values()):
        return {"enabled": False}
    hosted = replica.decode(payload)
    replica.install(engine, conn, hosted)
    conn.execute("""INSERT INTO history_sync_config (id, server, owner, enabled, baseline, last_sync)
                    VALUES (1, ?, ?, 1, ?, ?)""", (server, owner, json.dumps(replica.manifest(hosted)), time.time()))
    engine._audit(conn, "history_sync_restored", owner)
    return {"enabled": True, "uploaded": False, "downloaded": True,
            "conversations": len(hosted["history_sources"]), "messages": len(hosted["history_messages"])}


def synchronize(engine=None, *, discover=False):
    """Reconcile local and hosted history without discarding concurrent device changes."""
    if engine is None and history_path().is_file():
        engine = open_history()
    config = _configuration(engine) if engine is not None else None
    if (config is None and not discover) or (config is not None and not config["enabled"]):
        return {"enabled": False}
    server, token = _connection()
    if config is not None and config["server"] != server:
        raise sessions.SyncError("This archive is linked to a different CogniCore server.")
    endpoint = f"{server}/sync/history"
    with sessions._Lock("history-sync.lock", wait=0.25, stale=600):
        for _ in range(3):
            remote = sessions._request("GET", endpoint, token=token, timeout=45)
            if config is None:
                if remote.get("payload") is None:
                    return {"enabled": False}
                engine = engine or open_history()
                with engine._transaction(write=True) as conn:
                    if conn.execute("SELECT 1 FROM history_sync_config WHERE id=1").fetchone():
                        return {"enabled": False}
                    return _restore_new_device(engine, conn, server, remote)
            if remote.get("owner") != config["owner"]:
                raise sessions.SyncError("This archive is linked to a different CogniCore account.")
            hosted = replica.decode(remote.get("payload"))
            with engine._transaction() as conn:
                current = conn.execute("SELECT * FROM history_sync_config WHERE id=1 AND enabled=1").fetchone()
                if not current:
                    return {"enabled": False}
                local = replica.read(conn)
                baseline = json.loads(current["baseline"])
            merged = replica.merge(baseline, local, hosted)
            changed = merged != hosted or remote.get("payload") is None
            if changed:
                try:
                    sessions._request("PUT", endpoint, {"revision": remote["revision"], "payload": replica.encode(merged)},
                                      token=token, timeout=45)
                except sessions._HTTPError as exc:
                    if exc.status != 409:
                        raise
                    continue
            with engine._transaction(write=True) as conn:
                if not conn.execute("SELECT 1 FROM history_sync_config WHERE id=1 AND enabled=1").fetchone():
                    return {"enabled": False}
                latest = replica.read(conn)
                combined = replica.merge(replica.manifest(local), latest, merged) if latest != local else merged
                if combined != latest:
                    replica.install(engine, conn, combined)
                    engine._audit(conn, "history_sync_applied", "")
                conn.execute("UPDATE history_sync_config SET baseline=?, last_sync=? WHERE id=1",
                             (json.dumps(replica.manifest(merged), separators=(",", ":")), time.time()))
            return {"enabled": True, "uploaded": changed, "downloaded": combined != latest,
                    "conversations": len(combined["history_sources"]), "messages": len(combined["history_messages"])}
    raise sessions.SyncError("History changed on another device during sync. It will be retried.")


def sync_existing():
    if sessions.enabled():
        try:
            return synchronize(discover=True)
        except sessions._HTTPError as exc:
            if exc.status == 404:  # An older server may not have the history endpoint.
                return {"enabled": False}
            raise
        except sqlite3.Error:
            raise sessions.SyncError("The local history database could not be synchronized.") from None
    return {"enabled": False}


def schedule(*, mutated=False, on_login=False):
    """Start background sync or discover an opted-in archive for a new device."""
    path = history_path()
    if not sessions.enabled() or sessions.offline():
        return
    try:
        config = None
        if path.is_file():
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as conn:
                config = conn.execute("SELECT enabled, last_sync FROM history_sync_config WHERE id=1").fetchone()
                if config is None and conn.execute("SELECT 1 FROM history_batches LIMIT 1").fetchone():
                    return  # A populated, unbound archive needs explicit upload consent.
        if config is None:
            if mutated:
                return
            probe = sessions._data_dir() / "history-sync-probe"
            if not on_login and probe.exists() and time.time() - probe.stat().st_mtime < 60:
                return
            probe.touch()
        elif not config[0] or (not (mutated or on_login) and time.time() - (config[1] or 0) < 60):
            return
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "cognicore.history.sync"], **kwargs)  # nosec B603
    except (OSError, sqlite3.Error):
        return


def main():
    try:
        sync_existing()
    except (sessions.SyncError, OSError, KeyError, ValueError, sqlite3.Error) as exc:
        from cognicore.hooks import _log

        # Sync errors avoid including archive payloads in logs.
        _log(f"history sync deferred: {type(exc).__name__}")


if __name__ == "__main__":
    main()

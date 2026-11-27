"""Hosted history sync, using the actual authenticated server routes."""

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import jwt
import pytest
from fastapi.testclient import TestClient

from cognicore import sync as sessions
from cognicore.connectors import remote
from cognicore.history import HistoryEngine
from cognicore.history import replica, sync

DEMO = Path(__file__).resolve().parents[1] / "examples" / "chatgpt-history.json"
BASE = "https://history.example"
ALICE = "github:1001"
BOB = "github:2002"


def _headers(owner):
    token = jwt.encode(
        {"sub": owner, "typ": "access", "exp": int(time.time()) + 600},
        remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    account = {"owner": ALICE}
    calls = []

    client = TestClient(remote.app)
    try:
        def request(method, url, body=None, *, token, timeout):
            assert urlsplit(url).netloc == urlsplit(BASE).netloc
            calls.append(method)
            response = client.request(method, urlsplit(url).path, json=body, headers={"Authorization": f"Bearer {token}"})
            if response.status_code != 200:
                raise sessions._HTTPError(response.status_code, "test response")
            return response.json()

        monkeypatch.setattr(sync, "_connection", lambda: (BASE, _headers(account["owner"])["Authorization"][7:]))
        monkeypatch.setattr(sessions, "_request", request)
        yield client, account, calls
    finally:
        client.close()


def _archive(tmp_path, ref, text):
    archive = tmp_path / f"{ref}.json"
    archive.write_text(json.dumps([{"id": ref, "messages": [
        {"id": f"{ref}-message", "role": "user", "content": text, "create_time": 1740992400},
    ]}]), encoding="utf-8")
    return archive


def test_opt_in_full_archive_replication_and_source_deletion(tmp_path, cloud):
    _, account, calls = cloud
    first = HistoryEngine(tmp_path / "first.db")
    other = HistoryEngine(tmp_path / "other.db")
    report = first.import_archive(DEMO, consent=True, project="DIPY")
    claim, = first.search("memory-mapped arrays", kind="decision")
    first.attach_evidence(claim["experience_ref"], command="pytest", exit_code=0, output=b"observed",
                          timestamp="2025-03-03T09:00:00Z", source="local receipt")
    assert sync.synchronize(first) == {"enabled": False}
    with pytest.raises(PermissionError):
        sync.enable(first)
    assert calls == []
    assert sync.enable(first, consent=True)["uploaded"] is True
    restored_device = sync.synchronize(other, discover=True)
    assert restored_device["downloaded"] is True
    assert restored_device["uploaded"] is False
    original = other.inspect("dipy-followup-2025")
    assert original["messages"][-1]["attachments"]["references"][0]["name"] == "timings.csv"
    restored = other.experience(claim["experience_ref"])
    assert restored["evidence"][0]["source"] == "local receipt"
    assert restored["event_time"] == 1740992460
    assert other.status(report["id"])["messages"] == 9

    other.delete(conversation="dipy-architecture-2025")
    assert sync.synchronize(other)["uploaded"] is True
    assert sync.synchronize(first)["downloaded"] is True
    assert first.search("memory-mapped arrays", kind="decision") == []
    with pytest.raises(KeyError):
        first.experience(claim["experience_ref"])
    stale = HistoryEngine(tmp_path / "stale.db")
    stale.import_archive(DEMO, consent=True, project="DIPY")
    sync.enable(stale, consent=True)
    with pytest.raises(KeyError):
        stale.inspect("dipy-architecture-2025")

    account["owner"] = BOB
    writes = calls.count("PUT")
    with pytest.raises(sessions.SyncError, match="different CogniCore account"):
        sync.synchronize(first)
    with pytest.raises(sessions.SyncError, match="another sync account"):
        sync.enable(first, consent=True)
    assert calls.count("PUT") == writes


def test_two_devices_merge_imports_and_deduplicate_batch_ids(tmp_path, cloud):
    first = HistoryEngine(tmp_path / "first.db")
    second = HistoryEngine(tmp_path / "second.db")
    a = first.import_archive(DEMO, consent=True, project="DIPY")
    b = second.import_archive(DEMO, consent=True, project="DIPY")
    assert a["id"] != b["id"]
    sync.enable(first, consent=True)
    sync.enable(second, consent=True)
    assert second.status(b["id"])["id"] == a["id"]
    assert second.status(a["id"])["messages"] == 9
    assert len(second.status()) == 1
    first.import_archive(_archive(tmp_path, "offline-first", "We chose shared logs."), consent=True)
    second.import_archive(_archive(tmp_path, "offline-second", "We chose JSON output."), consent=True)
    sync.synchronize(first)
    sync.synchronize(second)
    sync.synchronize(first)
    assert first.inspect("offline-second")["messages"][0]["text"] == "We chose JSON output."
    assert second.inspect("offline-first")["messages"][0]["text"] == "We chose shared logs."

    second.delete(batch=b["id"])
    sync.synchronize(second)
    sync.synchronize(first)
    assert first.search("dipy") == []
    assert first.search("JSON output")


def test_snapshot_endpoint_requires_bearer_and_enforces_tenant_isolation(cloud, monkeypatch):
    client, _, _ = cloud
    blank = replica.encode(replica.decode(None))
    assert client.get("/sync/history").status_code == 401
    monkeypatch.setattr(remote, "INSECURE_AUTH_ENABLED", True)
    assert client.get("/sync/history", headers={"x-anthropic-client": "test"}).status_code == 401
    assert client.put("/sync/history", json={"revision": 0, "payload": blank}).status_code == 401
    alice, bob = _headers(ALICE), _headers(BOB)
    assert client.put("/sync/history", headers=alice, json={"revision": 0, "payload": "bad"}).status_code == 400
    accepted = client.put("/sync/history", headers=alice, json={"revision": 0, "payload": blank})
    assert accepted.status_code == 200
    assert accepted.json()["revision"] == 1
    assert client.put("/sync/history", headers=alice, json={"revision": 0, "payload": blank}).status_code == 409
    fetched = client.get("/sync/history", headers=alice)
    assert fetched.headers["Cache-Control"] == "no-store"
    assert fetched.json()["payload"] == blank
    other = client.get(f"/sync/history?owner={ALICE}", headers=bob).json()
    assert other["owner"] != fetched.json()["owner"]
    assert other["payload"] is None


def test_mutation_schedules_sync_only_after_enable(tmp_path, cloud, monkeypatch):
    monkeypatch.setattr(sessions, "enabled", lambda: True)
    monkeypatch.setattr(sessions, "offline", lambda: False)
    started = []
    monkeypatch.setattr(sync.subprocess, "Popen", lambda args, **kwargs: started.append(args))
    engine = sync.open_history()
    sync.schedule(mutated=True)
    assert started == []
    sync.enable(engine, consent=True)
    sync.schedule()
    assert started == []
    sync.schedule(mutated=True)
    assert [args[-1] for args in started] == ["cognicore.history.sync"]
    sync.disable(engine)
    sync.schedule(mutated=True)
    assert len(started) == 1


def test_discovery_waits_for_account_opt_in_and_keeps_unbound_archives_private(tmp_path, cloud):
    _, _, calls = cloud
    local = HistoryEngine(tmp_path / "local.db")
    local.import_archive(DEMO, consent=True, project="DIPY")
    assert sync.synchronize(discover=True) == {"enabled": False}
    assert not sync.history_path().exists()
    primary = HistoryEngine(tmp_path / "primary.db")
    assert sync.enable(primary, consent=True)["uploaded"] is True  # Empty opt-in also registers the account.
    writes = calls.count("PUT")
    assert sync.synchronize(local, discover=True) == {"enabled": False}
    assert calls.count("PUT") == writes
    assert local.inspect("dipy-architecture-2025")["messages"]
    paused = HistoryEngine(tmp_path / "paused.db")
    sync.disable(paused)
    previous = list(calls)
    assert sync.synchronize(paused, discover=True) == {"enabled": False}
    assert calls == previous
    assert sync.synchronize(discover=True)["enabled"] is True
    assert sync.history_path().exists()


def test_login_automatically_restores_the_account_archive(tmp_path, cloud, monkeypatch):
    primary = HistoryEngine(tmp_path / "primary.db")
    primary.import_archive(DEMO, consent=True)
    sync.enable(primary, consent=True)
    monkeypatch.setattr(sessions, "login", lambda server: {"login": "alice"})
    monkeypatch.setattr(sessions, "enabled", lambda: True)
    monkeypatch.setattr(sessions, "offline", lambda: False)
    started = []
    monkeypatch.setattr(sync.subprocess, "Popen", lambda args, **kwargs: started.append(args))
    assert sessions.main(["login"]) == 0
    assert [args[-1] for args in started] == ["cognicore.history.sync"]
    sync.main()  # Execute the scheduled entry point using the authenticated test transport.
    assert sync.open_history().inspect("dipy-architecture-2025")["messages"]


def test_client_retries_stale_write_without_losing_local_changes(tmp_path, cloud, monkeypatch):
    client, _, calls = cloud
    engine = HistoryEngine(tmp_path / "first.db")
    engine.import_archive(_archive(tmp_path, "before", "We chose the first format."), consent=True)
    sync.enable(engine, consent=True)
    engine.import_archive(_archive(tmp_path, "queued", "We chose the queued format."), consent=True)
    request = sessions._request
    raced = []

    def with_concurrent_update(method, url, body=None, *, token, timeout):
        if method == "PUT" and not raced:
            raced.append(True)
            headers = _headers(ALICE)
            current = client.get("/sync/history", headers=headers).json()
            accepted = client.put("/sync/history", headers=headers,
                                  json={"revision": current["revision"], "payload": current["payload"]})
            assert accepted.status_code == 200
            engine.import_archive(_archive(tmp_path, "during", "We chose the newer format."), consent=True)
        return request(method, url, body, token=token, timeout=timeout)

    monkeypatch.setattr(sessions, "_request", with_concurrent_update)
    prior = calls.count("PUT")
    assert sync.synchronize(engine)["messages"] == 3
    assert calls.count("PUT") == prior + 2
    assert engine.inspect("during")["messages"]
    restored = HistoryEngine(tmp_path / "restored.db")
    assert sync.enable(restored, consent=True)["messages"] == 3

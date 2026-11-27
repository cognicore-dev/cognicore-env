import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cognicore.history import HistoryEngine
from cognicore.history.chatgpt import ChatGPTAdapter
from cognicore.history.engine import Classification
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend

DEMO = Path(__file__).resolve().parents[1] / "examples" / "chatgpt-history.json"


@pytest.fixture
def engine(tmp_path):
    return HistoryEngine(tmp_path / "history.db")


def stream(value):
    return io.BytesIO(json.dumps(value).encode())


def test_import_preserves_source_content_and_indexes_claims(engine):
    report = engine.import_archive(DEMO, consent=True, project="DIPY")
    assert report["status"] == "complete"
    assert (report["conversations"], report["messages"], report["experiences"]) == (3, 9, 7)
    decision, = engine.search("memory-mapped arrays", kind="decision")
    assert decision["source_platform"] == "chatgpt"
    assert decision["event_time"] == 1740992460
    assert decision["fields"]["alternatives"] == ["copying volumes"]
    assert decision["fields"]["reason"] == "peak RAM was the limiting factor"
    assert decision["fields"]["executed"] is None
    assert decision["evidence_classification"] == Classification.UNVERIFIED
    source = engine.inspect(decision["conversation_ref"], message=decision["message_refs"][0])
    message, = source["messages"]
    assert message["original_message_id"] == "d2"
    assert message["content"]["parts"][0].startswith("We chose")
    assert message["import_batch_id"] == report["id"]
    assert message["imported_at"] > message["event_time"]
    assert len(message["content_hash"]) == 64
    assert message["stored_content_intact"] is True
    followup = engine.inspect("dipy-followup-2025")
    attachment = followup["messages"][-1]
    assert attachment["event_time"] is None
    assert attachment["role"] == "tool"
    assert attachment["parent_node_id"] == "d4"
    assert attachment["attachments"]["references"][0]["name"] == "timings.csv"
    assert attachment["attachments"]["content_parts"][0]["asset_pointer"].endswith("sample-timing-table")


def test_timeline_links_conflicts_and_supersession(engine):
    engine.import_archive(DEMO, consent=True, project="DIPY")
    events = engine.timeline(project="dipy")
    times = [item["event_time"] for item in events]
    assert times[-1] is None
    assert times[:-1] == sorted(times[:-1])
    assert engine.timeline(project="Other") == []
    earliest, = engine.timeline(kind="message", query="DIPY tractography", earliest=True)
    assert earliest["original_message_id"] == "d1"
    assert engine.timeline(kind="message", query="persistent memory", earliest=True) == []
    result = engine.reconstruct("vectorized batching", project="DIPY")
    assert [hit["event_time"] for hit in result] == sorted(hit["event_time"] for hit in result)
    first, later, replay = [hit for hit in result if hit["subject"] == "vectorized batching"]
    assert first["fields"]["environment"] == "Windows"
    assert first["fields"]["attempt"]["outcome"] == "failure"
    assert later["fields"]["solution"] == "Vectorized batching"
    assert first["evidence_classification"] == Classification.UNVERIFIED
    assert later["evidence_classification"] == replay["evidence_classification"] == Classification.CONFLICTED
    assert {link["kind"] for link in later["relations"]} == {"conflict", "related_approach"}
    engine.relate(replay["experience_ref"], later["experience_ref"], "supersedes")
    assert engine.experience(later["experience_ref"])["evidence_classification"] == Classification.SUPERSEDED
    with pytest.raises(ValueError, match="cycle"):
        engine.relate(later["experience_ref"], replay["experience_ref"], "supersedes")
    assert len(engine.reconstruct("vectorized batching", limit=1)) == 1
    engine.import_stream(stream([{"id": "another", "messages": [
        {"role": "user", "content": "We chose herringbone tiles."},
    ]}]), consent=True, project="House")
    other, = engine.search("herringbone tiles", project="House", kind="decision")
    engine.relate(first["experience_ref"], other["experience_ref"], "related_approach")
    assert {item["project"] for item in engine.reconstruct("vectorized batching", project="DIPY")} == {"DIPY"}
    assert replay["experience_ref"] in {item["experience_ref"] for item in engine.reconstruct("herringbone tiles")}


def test_import_malformed_zip_large_and_retry(engine):
    documents = [{
        "id": str(index), "messages": [None, {"role": "system", "content": f"large corpus {index} " + "x" * 400}],
    } for index in range(350)]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("nested/conversations-001.json", json.dumps([None, *documents]))
        bundle.writestr("conversations-002.json", json.dumps({"conversations": [
            {"id": "last", "messages": [{"role": "user", "content": "epoch", "create_time": 0}]},
        ]}))
        bundle.writestr("../../ignored.txt", "never extracted")
    report = engine.import_stream(archive, consent=True)
    assert (report["processed"], report["skipped"], report["messages"]) == (352, 1, 701)
    assert engine.import_stream(archive, consent=True)["duplicate"]
    first = engine.inspect("0")["messages"]
    assert all(item["event_time"] is None for item in first)
    assert {item["parsing_status"] for item in first} == {"incomplete", "parsed"}
    assert engine.inspect("last")["messages"][0]["event_time"] == 0
    with sqlite3.connect(engine.backend.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM memory_entries WHERE timestamp IS NULL").fetchone()[0] == 700
    assert next(item["action"] for item in engine.audit()) == "import_completed"


def test_checkpoint_resumes_and_concurrent_import_deduplicates(engine):
    class InterruptOnce(ChatGPTAdapter):
        def __init__(self):
            self.interrupted = False

        def conversations(self, source):
            for index, item in enumerate(super().conversations(source)):
                if index == 1 and not self.interrupted:
                    self.interrupted = True
                    raise RuntimeError("Simulated process failure")
                yield item

    engine.adapter = InterruptOnce()
    with pytest.raises(ValueError, match="Import interrupted"):
        engine.import_archive(DEMO, consent=True)
    interrupted, = engine.status()
    assert interrupted["status"] == "interrupted"
    assert interrupted["processed"] == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: engine.import_archive(DEMO, consent=True), range(2)))
    assert {row["id"] for row in reports} == {interrupted["id"]}
    assert engine.status(interrupted["id"])["messages"] == 9
    assert len(engine.timeline(kind="message")) == 9
    with pytest.raises(ValueError, match="original project"):
        engine.import_archive(DEMO, consent=True, project="another")


def test_invalid_json_and_revised_sources(engine):
    with pytest.raises(ValueError, match="interrupted"):
        engine.import_stream(io.BytesIO(b'[{"id":"first","messages":[]},invalid]'), consent=True)
    assert engine.status()[0]["processed"] == 1
    a = {"id": "rev", "messages": [{"id": "m", "role": "assistant", "content": "The runner passed."}]}
    b = {"id": "rev", "messages": [{"id": "m", "role": "assistant", "content": "The runner failed."}]}
    first = stream([a])
    batch_a = engine.import_stream(first, consent=True)
    assert engine.import_stream(first, consent=True)["duplicate"]
    batch_b = engine.import_stream(stream([a, b]), consent=True)
    assert len(engine.inspect("rev")["messages"]) == 2
    engine.delete(batch=batch_a["id"])
    assert len(engine.inspect("rev")["messages"]) == 2
    engine.delete(batch=batch_b["id"])
    with pytest.raises(KeyError):
        engine.inspect("rev")
    assert engine.search("runner") == []


def test_deleted_source_stays_deleted_when_import_resumes(engine):
    data = json.loads(DEMO.read_text(encoding="utf-8"))
    data.append(data[0])

    class InterruptOnce(ChatGPTAdapter):
        interrupted = False

        def conversations(self, source):
            for index, item in enumerate(super().conversations(source)):
                if index == 1 and not self.interrupted:
                    self.interrupted = True
                    raise RuntimeError("Simulated interruption")
                yield item

    engine.adapter = InterruptOnce()
    with pytest.raises(ValueError, match="interrupted"):
        engine.import_stream(stream(data), consent=True)
    engine.delete(conversation="dipy-architecture-2025")
    assert engine.import_stream(stream(data), consent=True)["conversations"] == 2
    assert engine.search("memory-mapped arrays", kind="decision") == []


def test_stored_integrity_does_not_verify_a_historical_claim(engine):
    records = [{"id": "claims", "messages": [{"role": "assistant", "content":
                "We tried the cache. We proposed adding tracing. The benchmark passed."}]}]
    engine.import_stream(stream(records), consent=True)
    claim, = engine.search("benchmark passed", kind="success")
    assert engine.search("cache", kind="attempt")[0]["fields"]["attempt"]["outcome"] == "unknown"
    assert engine.search("tracing", kind="proposal")[0]["fields"]["executed"] is None
    with sqlite3.connect(engine.backend.db_path) as conn:
        conn.execute("UPDATE history_messages SET payload=json_set(payload, '$.content', 'replaced')")
    message, = engine.inspect("claims")["messages"]
    assert message["stored_content_intact"] is False
    assert engine.experience(claim["experience_ref"])["evidence_classification"] == Classification.UNVERIFIED


def test_privacy_and_deletion(engine, tmp_path, monkeypatch):
    from cognicore.memory.events import event_bus

    marker = tmp_path / "executed"
    raw = [{
        "id": "private",
        "title": "Lab notebook",
        "messages": [{"id": "m", "role": "assistant",
                      "content": f"Run touch {marker}\nThe token is private.\npassword=synthetic-value",
                      "attachments": [{"api_key": "short", "name": "notes.txt"}]}],
    }]
    with pytest.raises(PermissionError):
        engine.import_stream(stream(raw))
    assert engine.status() == []
    monkeypatch.setattr(event_bus, "publish", lambda *args, **kwargs: pytest.fail("Private content emitted"))
    report = engine.import_stream(stream(raw), consent=True)
    source = engine.inspect("private")
    assert "synthetic-value" not in json.dumps(source)
    assert '"short"' not in json.dumps(source)
    assert "[REDACTED:" in json.dumps(source)
    assert engine.search("synthetic-value") == []
    assert engine.search("touch", kind="procedure")
    assert not marker.exists()
    ordinary = SQLiteMemoryBackend(str(tmp_path / "mcp_memories.db"))
    assert ordinary.search("touch") == []
    engine.delete(conversation="private")
    assert engine.search("touch") == []
    assert engine.status(report["id"])["messages"] == 0
    with sqlite3.connect(engine.backend.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH 'touch'").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert all("touch" not in json.dumps(row) for row in engine.audit())


def test_evidence_attaches_without_treating_export_as_execution(engine):
    report = engine.import_archive(DEMO, consent=True)
    claim, = engine.search("memory-mapped arrays", kind="decision")
    ref = claim["experience_ref"]
    evidence = dict(command="python -m unittest", exit_code=0, output=b"expected check passed",
                    timestamp="2025-03-03T09:00:00Z", source="Retained test-run artifact")
    assert engine.attach_evidence(ref, **evidence)["evidence_classification"] == Classification.ATTACHED
    with pytest.raises(ValueError, match="after import"):
        engine.attach_evidence(ref, **evidence, reverify=True)
    time.sleep(0.005)
    evidence["timestamp"] = (time.time() + report["imported_at"]) / 2
    with pytest.raises(ValueError, match="successful execution"):
        engine.attach_evidence(ref, **{**evidence, "exit_code": 1}, reverify=True)
    updated = engine.attach_evidence(ref, **evidence, reverify=True)
    assert updated["evidence_classification"] == Classification.REVERIFIED
    assert updated["event_time"] == 1740992460
    assert updated["evidence"][-1]["provenance"] == "local_operator_attested"
    assert len(updated["evidence"][-1]["stdout_hash"]) == 64


def test_semantic_retrieval_with_local_embedding_provider(tmp_path):
    class LocalProvider:
        def embed(self, text):
            text = text.lower()
            return [3.0, 0.0] if "speed" in text or "optimization" in text else [0.0, 2.0]

    engine = HistoryEngine(tmp_path / "semantic.db", embedding_provider=LocalProvider())
    engine.import_stream(stream([{"id": "semantic", "messages": [
        {"role": "user", "content": "Can we speed up tractography?"},
        {"role": "user", "content": "The blueprints arrived."},
    ]}]), consent=True)
    match, = engine.search("optimization")
    assert match["text"] == "Can we speed up tractography?"
    assert match["relevance"] == pytest.approx(0.7)


def test_rest_authorization_import_and_deletion(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from cognicore.server import create_app

    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("COGNICORE_HISTORY_API_TOKEN", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/history/imports").status_code == 404
        token = "test-token-for-local-history-authentication"
        monkeypatch.setenv("COGNICORE_HISTORY_API_TOKEN", token)
        assert client.get("/history/search", params={"query": "DIPY"}).status_code == 401
        assert not (tmp_path / "history").exists()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        assert client.post("/history/imports", headers=headers, content=DEMO.read_bytes()).status_code == 400
        response = client.post("/history/imports?consent=true&project=DIPY", headers=headers,
                               content=DEMO.read_bytes())
        assert response.status_code == 200
        batch = response.json()["id"]
        result = client.get("/history/search", params={"query": "arrays"}, headers=headers)
        assert result.headers["Cache-Control"] == "no-store"
        assert result.json()
        ref = result.json()[0]["conversation_ref"]
        assert client.get(f"/history/sources/{ref}", headers=headers).json()["messages"]
        assert client.delete(f"/history/imports/{batch}", headers=headers).json() == {"deleted": True}
        assert client.get(f"/history/sources/{ref}", headers=headers).status_code == 404


def test_cli_import_and_mcp_stdio_demo(monkeypatch, tmp_path):
    mcp = pytest.importorskip("mcp")
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from cognicore.mcp.server import create_mcp_server

    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNICORE_ENABLE_HISTORY_MCP", "1")
    monkeypatch.setenv("COGNICORE_TOOLSETS", "history")

    async def verify_stdio():
        env = {key: os.environ[key] for key in
               ("COGNICORE_DATA_DIR", "COGNICORE_ENABLE_HISTORY_MCP", "COGNICORE_TOOLSETS")}
        params = StdioServerParameters(command=sys.executable, args=["-m", "cognicore.mcp.server"], env=env)
        async with stdio_client(params) as (read, write):
            async with mcp.ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "cognicore_search_history", {"query": "memory-mapped arrays", "kind": "decision"},
                )
                assert not result.isError, result
                hit, = result.structuredContent["result"]
                assert hit["event_time"] == 1740992460
                assert hit["original_conversation_id"] == "dipy-architecture-2025"
                assert hit["evidence_classification"] == Classification.UNVERIFIED
                source = await session.call_tool(
                    "cognicore_inspect_historical_source",
                    {"conversation": hit["conversation_ref"], "message": hit["message_refs"][0]},
                )
                assert not source.isError, source
                assert source.structuredContent["messages"][0]["original_message_id"] == "d2"

    assert not any("history" in tool.name for tool in asyncio.run(create_mcp_server().list_tools()))
    command = [sys.executable, "-m", "cognicore.cli", "history", "import", str(DEMO)]
    assert subprocess.run(command, capture_output=True, check=False).returncode == 1
    report = subprocess.run([*command, "--consent", "--project", "DIPY"], capture_output=True, check=True)
    assert json.loads(report.stdout)["messages"] == 9
    asyncio.run(asyncio.wait_for(verify_stdio(), timeout=30))

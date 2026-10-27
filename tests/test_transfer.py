"""Tests for session save/resume/compress: the cross-agent memory transfer path."""
import json

import pytest

from cognicore.memory import SQLiteMemoryBackend
from cognicore.memory.context_preservation import compress_context, save_session


@pytest.fixture
def backend(tmp_path):
    return SQLiteMemoryBackend(str(tmp_path / "mem.db"))


CONV = [
    {"role": "user", "content": "Our goal is to fix intermittent 401s on /api/v2/orders. Ticket is AUTH-1423."},
    {"role": "assistant", "content": "We decided to switch to PyJWT 2.8.0 because python-jose is unmaintained."},
    {"role": "user", "content": "TODO: add a regression test in tests/test_auth.py."},
]


def test_two_saves_in_the_same_second_keep_separate_sessions(backend):
    a = json.loads(save_session(backend, CONV, session_name="A"))
    b = json.loads(save_session(backend, CONV[:1], session_name="B"))
    assert a["session_id"] != b["session_id"]
    with backend._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_compress_keep_last_n_zero_compresses_everything(backend):
    data = json.loads(compress_context(backend, CONV, keep_last_n=0))
    assert data["messages_compressed"] == len(CONV)
    assert "AUTH-1423" in data["summary"]


def test_compress_has_no_hardcoded_project_goal(backend):
    conv = [{"role": "user", "content": "Brand color is #111111 and pricing is $12/month."}]
    data = json.loads(compress_context(backend, conv, keep_last_n=0))
    assert "CogniCore MCP server" not in data["summary"]


class _CountingProvider:
    """Minimal embedding provider that records how it was called."""

    def __init__(self):
        self.single, self.batches = 0, []

    def embed(self, text):
        self.single += 1
        return [float(len(text)), 1.0]

    def embed_batch(self, texts):
        self.batches.append(len(texts))
        return [[float(len(t)), 1.0] for t in texts]

    @property
    def dimension(self):
        return 2


def test_store_many_matches_store_and_batches_embeddings(tmp_path):
    from cognicore.memory.base import MemoryEntry

    provider = _CountingProvider()
    b = SQLiteMemoryBackend(str(tmp_path / "m.db"), provider=provider)
    ids = b.store_many([MemoryEntry(text=f"fact number {i}", category="general") for i in range(5)])
    assert len(ids) == 5 and len(set(ids)) == 5
    assert provider.batches == [5] and provider.single == 0
    with b._get_conn() as conn:
        rows = conn.execute("SELECT text, embedding_json FROM memory_entries ORDER BY entry_id").fetchall()
    assert [r["text"] for r in rows] == [f"fact number {i}" for i in range(5)]
    assert all(r["embedding_json"] for r in rows)
    assert b.store_many([]) == []


def test_compress_reports_expansion_honestly(backend):
    conv = [{"role": "user", "content": "Launch is on November 15."}]
    data = json.loads(compress_context(backend, conv, keep_last_n=0))
    assert data["expanded"] is (data["tokens_after"] > data["tokens_before"])
    ratio = float(data["compression_ratio"].rstrip("x"))
    assert ratio == pytest.approx(data["tokens_before"] / max(1, data["tokens_after"]), abs=0.05)

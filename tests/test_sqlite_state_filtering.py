"""Regression tests for SQLiteMemoryBackend lifecycle-state filtering.

``MemoryEntry.is_searchable`` documents that archived and deleted entries must
not appear in search results. ``TFIDFMemoryBackend.search`` and
``SQLiteMemoryBackend.count`` already honor that rule; these tests pin the same
behavior on every SQLite search path.
"""

import pytest

from cognicore.memory.base import EmbeddingProvider, MemoryEntry, MemoryScope
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend


class _HashedBucketProvider(EmbeddingProvider):
    """Deterministic zero-dependency embeddings for the provider search path."""

    def embed(self, text):
        vec = [0.0] * 8
        for tok in text.lower().split():
            vec[sum(map(ord, tok)) % 8] += 1.0
        return vec

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]

    @property
    def dimension(self):
        return 8


def _store(backend, text="alpha deployment runbook", category="deploy", state=None):
    entry = MemoryEntry(text=text, category=category, scope=MemoryScope.GLOBAL)
    entry_id = backend.store(entry)
    if state is not None:
        assert backend.update(entry_id, state=state)
    return entry_id


@pytest.mark.parametrize("state", ["archived", "deleted"])
def test_lexical_search_excludes_inactive(tmp_path, state):
    backend = SQLiteMemoryBackend(str(tmp_path / "lexical.db"))
    _store(backend, state=state)
    assert backend.search("alpha deployment") == []


def test_embedding_search_excludes_archived(tmp_path):
    backend = SQLiteMemoryBackend(
        str(tmp_path / "provider.db"), provider=_HashedBucketProvider()
    )
    _store(backend, state="archived")
    assert backend.search("alpha deployment") == []


def test_empty_query_excludes_archived(tmp_path):
    backend = SQLiteMemoryBackend(str(tmp_path / "empty.db"))
    _store(backend, state="archived")
    assert backend.search("") == []


def test_fallback_search_excludes_archived(tmp_path):
    backend = SQLiteMemoryBackend(str(tmp_path / "fallback.db"))
    _store(backend, text="x", category="short", state="archived")
    with backend._get_conn() as conn:
        assert backend._fallback_search(conn, "x", 5, None, None, None) == []


def test_active_entry_remains_searchable_and_counted(tmp_path):
    backend = SQLiteMemoryBackend(str(tmp_path / "active.db"))
    _store(backend)
    results = backend.search("alpha deployment")
    assert [r.entry.text for r in results] == ["alpha deployment runbook"]
    assert backend.count() == 1

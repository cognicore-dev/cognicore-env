"""Regression tests for SQLiteMemoryBackend search score bounds (issue #115).

Raw BM25 (both the FTS5 ``rank`` column and the Okapi fallback) is unbounded
above. Before this fix, default-install searches (no embedding provider) could
return ``score > 1.0`` — e.g. 3.49 for an ordinary 3-token query — while the
hybrid path documents ``final_score = 0.7 * cosine + 0.3 * normalized_bm25``
as bounded by 1.0. These tests pin the bound on every lexical path and prove
the hybrid path still respects it now that ``_bm25_search`` normalizes.
"""

import hashlib
from typing import List

import pytest

from cognicore.memory.base import EmbeddingProvider, MemoryEntry, MemoryScope
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend

# The corpus from the issue's reproduction: ordinary, short engineering notes.
CORPUS = [
    "add null check before dereferencing user",
    "guard division by zero in ratio calc",
    "cache the expensive embedding lookup",
    "add null check before dereferencing pointer",
    "validate payload schema at ingress",
]

# Queries that produced raw (unnormalized) scores of 2.23 / 3.49 pre-fix.
BOUND_VIOLATING_QUERIES = [
    "null pointer crash",
    "guard division",
    "embedding cache expensive",
    "payload schema",
]


def _entries():
    return [
        MemoryEntry(
            text=t,
            category="crash",
            correct=True,
            scope=MemoryScope.GLOBAL,
            entry_id=f"e{i}",
        )
        for i, t in enumerate(CORPUS)
    ]


class _HashedBucketProvider(EmbeddingProvider):
    """Deterministic zero-dependency dense embeddings for the hybrid path.

    Each token hashes into one of 64 buckets; the vector is the bucket counts.
    Same-token texts therefore overlap strongly, unrelated texts weakly —
    enough to exercise real dot products through ``search()``.
    """

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * 64
        for tok in text.lower().split():
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16) % 64
            vec[h] += 1.0
        return vec

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]

    @property
    def dimension(self) -> int:
        return 64


@pytest.fixture
def backend(tmp_path):
    b = SQLiteMemoryBackend(str(tmp_path / "bounds.db"))
    for e in _entries():
        b.store(e)
    return b


def test_fts_path_scores_bounded(backend):
    """Default-install searches must never exceed the 0..1 score bound."""
    for query in BOUND_VIOLATING_QUERIES:
        results = backend.search(query, top_k=5)
        assert results, f"query {query!r} unexpectedly returned nothing"
        for r in results:
            assert 0.0 < r.score <= 1.0, (
                f"score {r.score} for query {query!r} violates the documented "
                f"0..1 bound (issue #115 regression)"
            )


def test_fts_path_ranking_and_top_score(backend):
    """Best match scores exactly 1.0 and ranking stays descending."""
    results = backend.search("null pointer crash", top_k=5)
    scores = [r.score for r in results]
    assert scores[0] == pytest.approx(1.0)
    assert scores == sorted(scores, reverse=True)
    # The top hit must be one of the null-check entries, not an unrelated one.
    assert "null check" in results[0].entry.text


def test_bm25_okapi_scores_bounded(backend):
    """The Okapi fallback used when FTS5 misses must respect the bound too."""
    with backend._get_conn() as conn:
        rows = conn.execute("SELECT * FROM memory_entries").fetchall()
    results = backend._bm25_search(rows, "null pointer dereferencing", 5, None, None, None)
    assert results
    for r in results:
        assert 0.0 < r.score <= 1.0


def test_hybrid_path_scores_bounded(tmp_path):
    """With a real provider, 0.7*cos + 0.3*norm_bm25 must stay <= 1.0.

    _bm25_search now returns normalized scores; the hybrid path divides them
    by their max again, which is idempotent. This pins the combined bound.
    """
    b = SQLiteMemoryBackend(
        str(tmp_path / "hybrid.db"), provider=_HashedBucketProvider()
    )
    for e in _entries():
        b.store(e)
    for query in BOUND_VIOLATING_QUERIES:
        results = b.search(query, top_k=5)
        for r in results:
            assert 0.0 < r.score <= 1.0, (
                f"hybrid score {r.score} for query {query!r} exceeds 1.0"
            )


def test_normalization_preserves_relative_gaps(backend):
    """Dividing by the max keeps the ranking and the ratios between scores."""
    raw = [3.5, 1.75, 0.875]
    from cognicore.memory.base import SearchResult

    fake = [SearchResult(entry=e, score=s, source="bm25") for e, s in zip(_entries(), raw)]
    normalized = backend._normalize_scores(fake)
    got = [r.score for r in normalized]
    assert got[0] == pytest.approx(1.0)
    assert got[1] == pytest.approx(0.5)
    assert got[2] == pytest.approx(0.25)
    assert got == sorted(got, reverse=True)


def test_normalize_scores_empty_and_zero(backend):
    """Degenerate inputs pass through untouched (no ZeroDivision, no data loss)."""
    assert backend._normalize_scores([]) == []
    from cognicore.memory.base import SearchResult

    zeros = [SearchResult(entry=e, score=0.0, source="bm25") for e in _entries()[:2]]
    assert backend._normalize_scores(zeros) == zeros

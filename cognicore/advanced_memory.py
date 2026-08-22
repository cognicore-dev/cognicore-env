"""
CogniCore Advanced Memory — Semantic similarity, decay, and adaptive retrieval.

Upgrades basic keyword-matching memory to an intelligent system that:
  - Uses TF-IDF for semantic similarity (zero external dependencies)
  - Implements memory decay (older memories fade)
  - Ranks memories by relevance and recency
  - Adapts retrieval strategy based on agent performance

Usage::

    from cognicore.advanced_memory import SemanticMemory

    mem = SemanticMemory(decay_rate=0.95)
    mem.store({"text": "phishing email", "category": "security", "correct": False})
    results = mem.semantic_search("suspicious email scam", top_k=3)
"""

from __future__ import annotations

import math
import time
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple


class SemanticMemory:
    """Advanced memory with TF-IDF similarity, decay, and adaptive retrieval.

    Parameters
    ----------
    max_size : int
        Maximum entries before eviction. Evicts lowest-relevance first.
    decay_rate : float
        Per-step decay multiplier (0.95 = 5% fade per step). 1.0 = no decay.
    similarity_threshold : float
        Minimum similarity score to consider a match (0.0-1.0).
    """

    def __init__(
        self,
        max_size: int = 10_000,
        decay_rate: float = 0.95,
        similarity_threshold: float = 0.01,
    ):
        self.entries: List[Dict[str, Any]] = []
        self.max_size = max_size
        self.decay_rate = decay_rate
        self.similarity_threshold = similarity_threshold
        self._step_count = 0
        self._idf_cache: Dict[str, float] = {}
        self._doc_freq: Dict[str, int] = defaultdict(int)
        self._total_docs = 0
        self._stats = {
            "total_stored": 0,
            "total_retrieved": 0,
            "semantic_hits": 0,
            "decay_evictions": 0,
        }

    # ------------------------------------------------------------------
    # Tokenization & TF-IDF
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Simple whitespace + punctuation tokenizer."""
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        tokens = text.split()
        # Remove very short tokens
        return [t for t in tokens if len(t) > 1]

    def _compute_tf(self, tokens: List[str]) -> Dict[str, float]:
        """Term frequency (normalized)."""
        counts = Counter(tokens)
        total = len(tokens) or 1
        return {t: c / total for t, c in counts.items()}

    def _compute_idf(self, term: str) -> float:
        """Inverse document frequency (smoothed)."""
        if self._total_docs == 0:
            return 0.0
        df = self._doc_freq.get(term, 0)
        # Smoothed IDF: log(1 + N / (1 + df))
        return math.log(1.0 + self._total_docs / (1.0 + df))

    def _tfidf_vector(self, text: str) -> Dict[str, float]:
        """Compute TF-IDF vector for a text."""
        tokens = self._tokenize(text)
        tf = self._compute_tf(tokens)
        return {t: tf[t] * self._compute_idf(t) for t in tf}

    def _cosine_similarity(
        self, vec_a: Dict[str, float], vec_b: Dict[str, float]
    ) -> float:
        """Cosine similarity between two sparse vectors."""
        common = set(vec_a.keys()) & set(vec_b.keys())
        if not common:
            return 0.0

        dot = sum(vec_a[t] * vec_b[t] for t in common)
        mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
        mag_b = math.sqrt(sum(v * v for v in vec_b.values()))

        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------

    def store(self, entry: Dict[str, Any], **kwargs) -> None:
        """Store an entry with semantic indexing.

        The entry should contain a ``text`` or ``prompt`` field for
        semantic matching, plus ``correct`` boolean and ``category``.
        """
        self._step_count += 1

        # Extract text for indexing, including category
        text = str(entry.get("text", entry.get("prompt", ""))) + " " + str(entry.get("category", ""))
        tokens = self._tokenize(str(text))

        # Update document frequencies
        for token in set(tokens):
            self._doc_freq[token] += 1
        self._total_docs += 1

        # Store with metadata
        enriched = {
            **entry,
            "_stored_step": self._step_count,
            "_stored_time": time.time(),
            "_relevance": 1.0,  # starts at full relevance
            "_tokens": tokens,
        }
        self.entries.append(enriched)
        self._stats["total_stored"] += 1

        # Apply decay to all existing entries
        for e in self.entries[:-1]:
            e["_relevance"] *= self.decay_rate

        # Evict if over capacity (remove lowest relevance)
        if len(self.entries) > self.max_size:
            self.entries.sort(key=lambda e: e["_relevance"])
            self.entries.pop(0)
            self._stats["decay_evictions"] += 1

    # ------------------------------------------------------------------
    # Semantic Search
    # ------------------------------------------------------------------

    def semantic_search(
        self, query: str, top_k: int = 5
    ) -> List[Tuple[Dict[str, Any], float]]:
        """Find entries semantically similar to the query.

        Returns list of (entry, similarity_score) tuples, sorted by
        combined relevance (similarity × decay).
        """
        self._stats["total_retrieved"] += 1

        if not self.entries:
            return []

        query_vec = self._tfidf_vector(query)
        if not query_vec:
            return []

        scored = []
        for entry in self.entries:
            entry_text = str(entry.get("text", entry.get("prompt", ""))) + " " + str(entry.get("category", ""))
            entry_vec = self._tfidf_vector(str(entry_text))
            sim = self._cosine_similarity(query_vec, entry_vec)

            if sim >= self.similarity_threshold:
                # Rank by combined (similarity × decay), but return raw sim
                # so consumers see actual match quality
                combined = sim * entry.get("_relevance", 1.0)
                scored.append((entry, sim, combined))
                self._stats["semantic_hits"] += 1

        # Sort by combined score (decay-weighted) but return raw sim
        scored.sort(key=lambda x: -x[2])
        return [(entry, sim) for entry, sim, _ in scored[:top_k]]

    def recall(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Simplified recall — returns entries without scores."""
        results = self.semantic_search(query, top_k)
        return [
            {k: v for k, v in entry.items() if not k.startswith("_")}
            for entry, _ in results
        ]

    # ------------------------------------------------------------------
    # Ranked Retrieval
    # ------------------------------------------------------------------

    def best_actions(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Retrieve the best past actions for similar situations."""
        results = self.semantic_search(query, top_k=20)
        # Fallback: if semantic search found nothing, try category match
        if not results:
            results = [
                (e, 1.0) for e in self.entries
                if e.get("category", "").lower() == query.lower()
            ]
        successes = [(e, s) for e, s in results if e.get("correct") is True]
        successes.sort(key=lambda x: -x[1])
        return [
            {k: v for k, v in e.items() if not k.startswith("_")}
            for e, _ in successes[:top_k]
        ]

    def retrieve_successes(self, category: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Alias for best_actions."""
        return self.best_actions(category, top_k)

    def retrieve_failures(self, category: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Alias for worst_actions."""
        return self.worst_actions(category, top_k)

    def worst_actions(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Retrieve the worst past actions (what NOT to do)."""
        results = self.semantic_search(query, top_k=20)
        # Fallback: if semantic search found nothing, try category match
        if not results:
            results = [
                (e, 1.0) for e in self.entries
                if e.get("category", "").lower() == query.lower()
            ]
        failures = [(e, s) for e, s in results if e.get("correct") is False]
        failures.sort(key=lambda x: -x[1])
        return [
            {k: v for k, v in e.items() if not k.startswith("_")}
            for e, _ in failures[:top_k]
        ]

    # ------------------------------------------------------------------
    # Adaptive Strategy
    # ------------------------------------------------------------------

    def get_adaptive_context(
        self, query: str, agent_accuracy: float = 0.5
    ) -> Dict[str, Any]:
        """Build context that adapts based on agent performance.

        - Low accuracy → more failure examples (learn from mistakes)
        - High accuracy → more success examples (reinforce)
        - Medium → balanced mix
        """
        if agent_accuracy < 0.4:
            # Struggling — show mostly failures to learn from
            failures = self.worst_actions(query, top_k=3)
            successes = self.best_actions(query, top_k=1)
            strategy = "learning_from_mistakes"
        elif agent_accuracy > 0.8:
            # Doing well — reinforce good patterns
            successes = self.best_actions(query, top_k=3)
            failures = self.worst_actions(query, top_k=1)
            strategy = "reinforcing_success"
        else:
            # Balanced
            successes = self.best_actions(query, top_k=2)
            failures = self.worst_actions(query, top_k=2)
            strategy = "balanced"

        return {
            "strategy": strategy,
            "successes": successes,
            "failures": failures,
            "memory_size": len(self.entries),
            "agent_accuracy": agent_accuracy,
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Return memory statistics including semantic search metrics."""
        total = len(self.entries)
        successes = sum(1 for e in self.entries if e.get("correct", False))
        avg_relevance = (
            sum(e["_relevance"] for e in self.entries) / total if total else 0
        )

        return {
            "total_entries": total,
            "successes": successes,
            "failures": total - successes,
            "avg_relevance": round(avg_relevance, 4),
            "vocabulary_size": len(self._doc_freq),
            "total_stored": self._stats["total_stored"],
            "total_retrieved": self._stats["total_retrieved"],
            "semantic_hits": self._stats["semantic_hits"],
            "decay_evictions": self._stats["decay_evictions"],
            "groups": sorted(list(set(e.get("category") for e in self.entries if e.get("category")))),
        }

    def clear(self) -> None:
        """Clear all entries and indices."""
        self.entries.clear()
        self._doc_freq.clear()
        self._total_docs = 0
        self._step_count = 0

    # ------------------------------------------------------------------
    # Opt 2: Memory Consolidation
    # ------------------------------------------------------------------

    def consolidate(self, threshold: int = 1000) -> int:
        """Merge similar failure/success entries to reduce memory bloat.

        After ``threshold`` entries, groups entries by category + correct
        status and merges them into high-confidence summary entries.

        Returns the number of entries removed by consolidation.
        """
        if len(self.entries) < threshold:
            return 0

        from collections import defaultdict
        buckets: dict = defaultdict(list)
        for e in self.entries:
            key = (e.get("category", ""), e.get("correct", False))
            buckets[key].append(e)

        removed = 0
        new_entries = []
        for (cat, correct), group in buckets.items():
            if len(group) <= 5:
                # Keep small groups as-is
                new_entries.extend(group)
                continue

            # Keep the 3 most recent entries verbatim
            group.sort(key=lambda e: e.get("_stored_step", 0))
            keep = group[-3:]
            merge_candidates = group[:-3]

            # Consolidate the rest into a single summary entry
            actions = defaultdict(int)
            for e in merge_candidates:
                action = e.get("action", e.get("predicted", "unknown"))
                actions[action] += 1

            best_action = max(actions, key=actions.get) if actions else "unknown"
            summary = {
                "text": f"[consolidated] {cat} ({len(merge_candidates)} entries)",
                "category": cat,
                "correct": correct,
                "action": best_action,
                "consolidated_count": len(merge_candidates),
                "_stored_step": self._step_count,
                "_stored_time": __import__("time").time(),
                "_relevance": sum(e.get("_relevance", 0.5) for e in merge_candidates) / len(merge_candidates),
                "_tokens": self._tokenize(cat),
            }
            new_entries.append(summary)
            new_entries.extend(keep)
            removed += len(merge_candidates) - 1  # -1 for the summary

        self.entries = new_entries
        return removed

    # ------------------------------------------------------------------
    # Opt 4: Export for research
    # ------------------------------------------------------------------

    def export_jsonl(self, output_path: str) -> int:
        """Export memory entries as JSONL for research / training data.

        Strips internal fields (``_*``) and writes one JSON object per line.

        Parameters
        ----------
        output_path : str
            Path to write the JSONL file.

        Returns
        -------
        int
            Number of entries exported.
        """
        import json
        from pathlib import Path

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        count = 0
        with open(path, "w", encoding="utf-8") as f:
            for entry in self.entries:
                clean = {k: v for k, v in entry.items() if not k.startswith("_")}
                f.write(json.dumps(clean, default=str) + "\n")
                count += 1

        return count

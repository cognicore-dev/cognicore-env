import time
import logging
from typing import List, Optional, Dict, Any

from cognicore.memory.base import MemoryBackend, MemoryEntry, SearchResult, MemoryScope
from cognicore.memory.tfidf_backend import TFIDFMemoryBackend

logger = logging.getLogger("cognicore.hybrid")

class HybridMemoryBackend(MemoryBackend):
    """Hybrid memory backend fusing sparse (TF-IDF) and dense (vector) retrievals.
    
    Uses Reciprocal Rank Fusion (RRF) combined with utility score and recency
    weighting to produce robust, context-aware memory retrieval. Degrades gracefully
    to pure TF-IDF if dense backend is unavailable.
    """

    def __init__(self,
                 sparse_backend: Optional[MemoryBackend] = None,
                 dense_backend: Optional[MemoryBackend] = None,
                 rrf_k: int = 60,
                 w_utility: float = 0.3,
                 w_recency: float = 0.2):
        self.sparse_backend = sparse_backend or TFIDFMemoryBackend()
        self.dense_backend = dense_backend
        self.rrf_k = rrf_k
        self.w_utility = w_utility
        self.w_recency = w_recency

        if self.dense_backend is None:
            logger.info("No dense memory backend provided. Hybrid retrieval will fall back to TF-IDF only.")

    def store(self, entry: MemoryEntry) -> str:
        """Store entry in both sparse and dense backends to keep them in sync."""
        # Ensure entry_id is set before storing in both to keep IDs synchronized
        if not entry.entry_id:
            import uuid
            entry.entry_id = f"mem_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
            
        entry_id = self.sparse_backend.store(entry)
        
        if self.dense_backend:
            try:
                self.dense_backend.store(entry)
            except Exception as e:
                logger.warning(f"Failed to store entry in dense backend: {e}. Desync might occur.")
                
        return entry_id

    def search(self, 
               query: str, 
               top_k: int = 5,
               category: Optional[str] = None,
               scope: Optional[MemoryScope] = None,
               scope_id: Optional[str] = None,
               question_timestamp: Optional[float] = None) -> List[SearchResult]:
        """Perform hybrid search using RRF and utility scoring."""
        
        # If no dense backend, fall back immediately to sparse search
        if not self.dense_backend:
            return self.sparse_backend.search(
                query, top_k=top_k, category=category, scope=scope, scope_id=scope_id, 
                question_timestamp=question_timestamp
            )

        # Retrieve candidates from both backends (retrieve more than top_k for better fusion)
        candidate_limit = top_k * 3
        try:
            sparse_res = self.sparse_backend.search(
                query, top_k=candidate_limit, category=category, scope=scope, scope_id=scope_id,
                question_timestamp=question_timestamp
            )
        except Exception as e:
            logger.warning(f"Sparse search failed: {e}")
            sparse_res = []

        try:
            dense_res = self.dense_backend.search(
                query, top_k=candidate_limit, category=category, scope=scope, scope_id=scope_id,
                question_timestamp=question_timestamp
            )
        except Exception as e:
            logger.warning(f"Dense search failed: {e}")
            dense_res = []

        # Reciprocal Rank Fusion (RRF)
        # We index candidates by entry_id
        rrf_scores: Dict[str, float] = {}
        entry_map: Dict[str, MemoryEntry] = {}

        # 1. Process sparse ranks
        for rank, res in enumerate(sparse_res, start=1):
            eid = res.entry.entry_id
            entry_map[eid] = res.entry
            rrf_scores[eid] = rrf_scores.get(eid, 0.0) + (1.0 / (self.rrf_k + rank))

        # 2. Process dense ranks
        for rank, res in enumerate(dense_res, start=1):
            eid = res.entry.entry_id
            entry_map[eid] = res.entry
            rrf_scores[eid] = rrf_scores.get(eid, 0.0) + (1.0 / (self.rrf_k + rank))

        # 3. Apply utility and recency boosts
        final_results: List[SearchResult] = []
        for eid, rrf_score in rrf_scores.items():
            entry = entry_map[eid]
            
            # Boost based on utility score (clamped to [-1.0, 1.0])
            utility_boost = 1.0 + (self.w_utility * entry.utility_score)
            
            # Boost based on recency (relevance decays over time in sparse backend)
            recency_boost = 1.0 + (self.w_recency * entry.relevance)
            
            final_score = rrf_score * utility_boost * recency_boost
            
            final_results.append(SearchResult(
                entry=entry,
                score=final_score,
                source="hybrid"
            ))

        # Sort and return top_k
        final_results.sort(key=lambda x: x.score, reverse=True)
        return final_results[:top_k]

    def get_by_category(self, 
                        category: str, 
                        top_k: int = 5,
                        success_filter: Optional[bool] = None) -> List[MemoryEntry]:
        """Category-exact retrieval."""
        return self.sparse_backend.get_by_category(category, top_k, success_filter)

    def count(self) -> int:
        """Total active entries."""
        return self.sparse_backend.count()

    def clear(self) -> None:
        """Clear both backends."""
        self.sparse_backend.clear()
        if self.dense_backend:
            self.dense_backend.clear()

    def update(self, entry_id: str, **fields: Any) -> bool:
        """Update fields in both backends."""
        ok = self.sparse_backend.update(entry_id, **fields)
        if self.dense_backend:
            ok_dense = self.dense_backend.update(entry_id, **fields)
            ok = ok or ok_dense
        return ok

    def delete(self, entry_id: str) -> bool:
        """Delete from both backends."""
        ok = self.sparse_backend.delete(entry_id)
        if self.dense_backend:
            ok_dense = self.dense_backend.delete(entry_id)
            ok = ok or ok_dense
        return ok

    def save(self, path: Optional[str] = None) -> None:
        """Save state to disk."""
        self.sparse_backend.save(path)
        if self.dense_backend:
            self.dense_backend.save(path)

    def load(self, path: Optional[str] = None) -> None:
        """Load state from disk."""
        self.sparse_backend.load(path)
        if self.dense_backend:
            self.dense_backend.load(path)
            
    def _get_all_entries(self) -> List[MemoryEntry]:
        """Internal accessor for all entries."""
        return self.sparse_backend._get_all_entries()

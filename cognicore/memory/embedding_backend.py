import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import math

from cognicore.memory.base import MemoryBackend, MemoryEntry, MemoryState, SearchResult, MemoryScope, EmbeddingProvider
from cognicore.memory.temporal import TemporalResolutionEngine
from cognicore.memory.events import event_bus

logger = logging.getLogger(__name__)

class BasicEmbeddingBackend(MemoryBackend):
    """
    In-memory vector store using a pluggable EmbeddingProvider.
    Computes cosine similarity for search.
    Stores data in JSON (vectors are stored as lists of floats).
    """

    def __init__(self, provider: EmbeddingProvider, max_size: int = 10000, persistence_path: Optional[str] = None, reranker=None):
        self.provider = provider
        self.reranker = reranker
        self.max_size = max_size
        self.persistence_path = Path(persistence_path).expanduser() if persistence_path else None
        
        self.entries: List[MemoryEntry] = []
        self.vectors: List[List[float]] = []
        self._next_id = 1
        
        self.temporal_engine = TemporalResolutionEngine(self)

        self.load()

    def store(self, entry: MemoryEntry) -> str:
        if len(self.entries) >= self.max_size:
            # simple eviction: remove oldest
            self.entries.pop(0)
            self.vectors.pop(0)
            
        entry_id = str(self._next_id)
        self._next_id += 1
        entry.entry_id = entry_id
        
        # We embed the "text" by default. If text is empty, embed action.
        text_to_embed = entry.text or entry.action
        vector = self.provider.embed(text_to_embed)
        
        # normalize vector for cosine similarity via dot product
        norm = sum(x*x for x in vector) ** 0.5
        if norm > 0:
            vector = [x / norm for x in vector]
            
        self.entries.append(entry)
        self.vectors.append(vector)
        self.save()
        
        event_bus.publish("on_store", entry=entry, entry_id=entry_id)
        return entry_id

    def get_superseding(self, entry_id: str) -> Optional[MemoryEntry]:
        """Check if any memory entry explicitly supersedes the given entry_id."""
        for entry in self.entries:
            if entry.supersedes == entry_id:
                return entry
        return None

    def search(self, query: str, top_k: int = 5,
               category: Optional[str] = None,
               scope: Optional[MemoryScope] = None,
               scope_id: Optional[str] = None,
               question_timestamp: Optional[float] = None,
               candidate_k: int = 20) -> List[SearchResult]:
        if not self.entries:
            return []
            
        query_vec = self.provider.embed(query)
        norm = sum(x*x for x in query_vec) ** 0.5
        if norm > 0:
            query_vec = [x / norm for x in query_vec]
            
        results = []
        for entry, vector in zip(self.entries, self.vectors):
            if not entry.is_searchable:
                continue
            if category and entry.category != category:
                continue
            if scope and entry.scope != scope:
                continue
            if scope_id and entry.scope_id != scope_id:
                continue
                
            # Dot product (both are normalized)
            score = sum(a * b for a, b in zip(query_vec, vector))
            results.append(SearchResult(entry=entry, score=score, source="embedding"))
            
        # Sort by embedding score descending
        results.sort(key=lambda x: x.score, reverse=True)
        
        # Apply Reranker if available
        if self.reranker and len(results) > 0:
            candidates = results[:candidate_k]
            candidate_texts = [res.entry.text or res.entry.action for res in candidates]
            rerank_scores = self.reranker.rerank(query, candidate_texts)
            
            for i, res in enumerate(candidates):
                res.score = rerank_scores[i]
                res.source = "cross-encoder"
                
            # Re-sort candidates by cross-encoder score
            candidates.sort(key=lambda x: x.score, reverse=True)
            results = candidates + results[candidate_k:] # keep the rest for temporal engine just in case
            
        # Apply temporal resolution
        resolved_results = self.temporal_engine.resolve(results, question_timestamp)
        final_results = resolved_results[:top_k]
        
        event_bus.publish("on_search", query=query, top_k=top_k, results=final_results)
        return final_results

    def get_by_category(self, category: str, top_k: int = 5,
                        success_filter: Optional[bool] = None) -> List[MemoryEntry]:
        results = []
        # Return most recent first
        for entry in reversed(self.entries):
            if entry.category != category:
                continue
            if success_filter is not None and entry.correct != success_filter:
                continue
            results.append(entry)
            if len(results) >= top_k:
                break
        return results

    def count(self) -> int:
        return len(self.entries)

    def clear(self) -> None:
        self.entries = []
        self.vectors = []
        self._next_id = 1
        self.save()

    # ------------------------------------------------------------------
    # v2 Lifecycle Methods
    # ------------------------------------------------------------------

    def update(self, entry_id: str, **fields) -> bool:
        """Update fields on an existing entry."""
        for entry in self.entries:
            if entry.entry_id == entry_id:
                for key, value in fields.items():
                    if hasattr(entry, key):
                        setattr(entry, key, value)
                self.save()
                return True
        return False

    def get_by_id(self, entry_id: str):
        """Retrieve a single entry by its ID."""
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def delete(self, entry_id: str) -> bool:
        """Hard-delete an entry and its vector."""
        for i, entry in enumerate(self.entries):
            if entry.entry_id == entry_id:
                self.entries.pop(i)
                self.vectors.pop(i)
                self.save()
                return True
        return False

    def save(self, path: Optional[Union[str, Path]] = None) -> None:
        target = path or self.persistence_path
        if not target:
            return
        target_path = Path(target)
        if target_path.is_dir() or (not target_path.suffix and not target_path.exists()):
            target_path.mkdir(parents=True, exist_ok=True)
            file_path = target_path / "memory.json"
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            file_path = target_path
        self.persistence_path = file_path

        data = {
            "entries": [e.to_dict() for e in self.entries],
            "vectors": self.vectors,
            "next_id": self._next_id
        }
        try:
            file_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to save basic embedding memory to {file_path}: {e}")

    def load(self, path: Optional[Union[str, Path]] = None) -> None:
        target = path or self.persistence_path
        if not target:
            return
        target_path = Path(target)
        if target_path.is_dir():
            file_path = target_path / "memory.json"
            if not file_path.exists():
                candidates = list(target_path.glob("*memory.json"))
                if candidates:
                    file_path = candidates[0]
        else:
            file_path = target_path

        if not file_path.exists():
            return
        self.persistence_path = file_path

        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            self.entries = [MemoryEntry.from_dict(d) for d in data.get("entries", [])]
            self.vectors = data.get("vectors", [])
            self._next_id = data.get("next_id", 1)
        except Exception as e:
            logger.warning(f"Failed to load basic embedding memory from {file_path}: {e}")

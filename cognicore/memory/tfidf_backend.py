import math
import time
import re
import json
from pathlib import Path
import threading
import logging
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .base import MemoryBackend, MemoryEntry, MemoryState, SearchResult, MemoryScope
from .events import event_bus

class TFIDFMemoryBackend(MemoryBackend):
    """
    Zero-dependency memory backend using TF-IDF similarity.
    
    This is the default backend. It provides semantic search using
    sparse TF-IDF vectors and cosine similarity, as well as exact
    category matching. It implements memory decay (older memories fade)
    and JSON-based persistence.
    
    For production use with better semantic matching, install
    `cognicore[embeddings]` and use `EmbeddingMemoryBackend` instead.
    """


    def _with_lock(func):
        def wrapper(self, *args, **kwargs):
            with self._lock:
                return func(self, *args, **kwargs)
        return wrapper

    def __init__(
        self,
        max_size: int = 10_000,
        decay_rate: float = 0.95,
        similarity_threshold: float = 0.01,
        persistence_path: Optional[str] = None,
    ):
        self.entries: List[MemoryEntry] = []
        self.max_size = max_size
        self.decay_rate = decay_rate
        self.similarity_threshold = similarity_threshold
        self.persistence_path = persistence_path
        
        self._step_count = 0
        self._doc_freq: Dict[str, int] = defaultdict(int)
        self._total_docs = 0
        self._lock = threading.RLock()
        
        # Load existing state if available
        if self.persistence_path:
            self.load()

    # ------------------------------------------------------------------
    # Tokenization & TF-IDF
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Simple whitespace + punctuation tokenizer."""
        if not text:
            return []
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        tokens = text.split()
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
        return math.log(1.0 + self._total_docs / (1.0 + df))

    def _tfidf_vector(self, text: str) -> Dict[str, float]:
        """Compute TF-IDF vector for a text."""
        tokens = self._tokenize(text)
        tf = self._compute_tf(tokens)
        return {t: tf[t] * self._compute_idf(t) for t in tf}

    def _cosine_similarity(self, vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
        """Cosine similarity between two sparse vectors."""
        common = set(vec_a.keys()) & set(vec_b.keys())
        if not common:
            return 0.0
        
        dot_product = sum(vec_a[t] * vec_b[t] for t in common)
        norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
        norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
        
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot_product / (norm_a * norm_b)

    # ------------------------------------------------------------------
    # MemoryBackend Interface
    # ------------------------------------------------------------------

    @_with_lock
    def store(self, entry: MemoryEntry) -> str:
        """Store an entry, update TF-IDF stats, and apply decay."""
        self._step_count += 1
        entry.metadata["_inserted_at_step"] = self._step_count
                
        # Initialize auto-fields if empty
        if not entry.entry_id:
            entry.entry_id = f"mem_{int(time.time()*1000)}_{self._step_count}"
        if not entry.timestamp:
            entry.timestamp = time.time()
        if not entry.last_accessed:
            entry.last_accessed = entry.timestamp
            
        # Update vocabulary
        combined_text = f"{entry.category} {entry.text}"
        tokens = set(self._tokenize(combined_text))
        for t in tokens:
            self._doc_freq[t] += 1
        self._total_docs += 1
        
        # Store internal vector cache
        entry.metadata["_tfidf_vector"] = self._tfidf_vector(combined_text)
        
        self.entries.append(entry)
        
        # Evict if over size
        if len(self.entries) > self.max_size:
            # Sort by relevance ascending
            self.entries.sort(key=lambda x: x.relevance)
            evicted = self.entries.pop(0)
            
            # Decrement vocab counts
            evicted_text = f"{evicted.category} {evicted.text}"
            evicted_tokens = set(self._tokenize(evicted_text))
            for t in evicted_tokens:
                self._doc_freq[t] -= 1
                if self._doc_freq[t] <= 0:
                    del self._doc_freq[t]
            self._total_docs -= 1
            
        self.save()
        event_bus.publish("on_store", entry=entry, entry_id=entry.entry_id)
        return entry.entry_id

    @_with_lock
    def search(
        self,
        query: str,
        top_k: int = 5,
        category: Optional[str] = None,
        scope: Optional[MemoryScope] = None,
        scope_id: Optional[str] = None,
        question_timestamp: Optional[float] = None
    ) -> List[SearchResult]:
        """Semantic search with optional filters."""
        is_empty_query = not query or not query.strip()
        query_vec = self._tfidf_vector(query) if not is_empty_query else {}
        results: List[SearchResult] = []
        
        for entry in self.entries:
            # Skip archived/deleted entries
            if not entry.is_searchable:
                continue
            # Filter by scope
            if scope and entry.scope != scope:
                continue
            if scope_id and entry.scope_id != scope_id:
                continue
            # Filter by category
            if category and entry.category != category:
                continue
                
            step_diff = self._step_count - entry.metadata.get("_inserted_at_step", self._step_count)
            dynamic_relevance = entry.relevance * (self.decay_rate ** step_diff)

            if is_empty_query:
                # If query is empty, just return all matching entries sorted by relevance
                results.append(SearchResult(entry=entry, score=dynamic_relevance, source="semantic"))
            else:
                entry_vec = entry.metadata.get("_tfidf_vector", {})
                if not entry_vec:
                    # Fallback if cached vector is missing
                    entry_vec = self._tfidf_vector(f"{entry.category} {entry.text}")
                    entry.metadata["_tfidf_vector"] = entry_vec
                    
                raw_sim = self._cosine_similarity(query_vec, entry_vec)
                if raw_sim >= self.similarity_threshold:
                    # Score combines semantic similarity with relevance (recency)
                    final_score = raw_sim * dynamic_relevance
                    results.append(SearchResult(entry=entry, score=final_score, source="semantic"))
                
        # Sort descending by score
        results.sort(key=lambda x: x.score, reverse=True)
        final_results = results[:top_k]
        
        event_bus.publish("on_search", query=query, top_k=top_k, results=final_results)
        return final_results

    @_with_lock
    def get_by_category(
        self,
        category: str,
        top_k: int = 5,
        success_filter: Optional[bool] = None
    ) -> List[MemoryEntry]:
        """Category-exact retrieval, sorted by recency/relevance."""
        matches = []
        for entry in self.entries:
            if entry.category == category:
                if success_filter is None or entry.correct == success_filter:
                    matches.append(entry)
                    
        # Sort by relevance (most relevant/recent first)
        matches.sort(key=lambda x: x.relevance, reverse=True)
        return matches[:top_k]

    @_with_lock
    def count(self) -> int:
        return len([e for e in self.entries if e.is_searchable])

    # ------------------------------------------------------------------
    # v2 Lifecycle Methods
    # ------------------------------------------------------------------

    @_with_lock
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

    @_with_lock
    def delete(self, entry_id: str) -> bool:
        """Hard-delete an entry from the backend."""
        for i, entry in enumerate(self.entries):
            if entry.entry_id == entry_id:
                removed = self.entries.pop(i)
                # Decrement vocab counts
                removed_text = f"{removed.category} {removed.text}"
                removed_tokens = set(self._tokenize(removed_text))
                for t in removed_tokens:
                    self._doc_freq[t] -= 1
                    if self._doc_freq[t] <= 0:
                        del self._doc_freq[t]
                self._total_docs -= 1
                self.save()
                return True
        return False

    @_with_lock
    def clear(self) -> None:
        self.entries.clear()
        self._doc_freq.clear()
        self._total_docs = 0
        self._lock = threading.RLock()
        self._step_count = 0
        self.save()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        if not self.persistence_path:
            return
            
        try:
            path = Path(self.persistence_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            
            data = {
                "max_size": self.max_size,
                "decay_rate": self.decay_rate,
                "_step_count": self._step_count,
                "_total_docs": self._total_docs,
                "_doc_freq": self._doc_freq,
                "entries": [e.to_dict() for e in self.entries]
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            logging.getLogger(__name__).exception("TFIDF save failed")

    def load(self) -> None:
        if not self.persistence_path:
            return
            
        path = Path(self.persistence_path)
        if not path.exists():
            return
            
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            self.max_size = data.get("max_size", self.max_size)
            self.decay_rate = data.get("decay_rate", self.decay_rate)
            self._step_count = data.get("_step_count", 0)
            self._total_docs = data.get("_total_docs", 0)
            self._doc_freq = defaultdict(int, data.get("_doc_freq", {}))
            
            raw_entries = data.get("entries", [])
            self.entries = [MemoryEntry.from_dict(d) for d in raw_entries]
        except Exception as e:
            logging.getLogger(__name__).exception("TFIDF load failed")

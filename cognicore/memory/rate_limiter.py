import logging
import time
from typing import List, Optional
from collections import deque

from cognicore.memory.base import MemoryBackend, MemoryEntry, SearchResult, MemoryScope

logger = logging.getLogger(__name__)

class RateLimitExceeded(Exception):
    """Exception raised when a memory operation exceeds rate limits."""
    pass

class RateLimitedMemoryBackend(MemoryBackend):
    """
    Wrapper for MemoryBackend that enforces rate limiting on store and search calls.
    Uses a simple sliding window algorithm.
    """

    def __init__(self, backend: MemoryBackend, max_calls: int = 100, window_seconds: float = 60.0):
        self.backend = backend
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        
        self._call_timestamps: deque = deque()

    def _check_rate_limit(self) -> None:
        now = time.time()
        
        # Remove timestamps outside the window
        while self._call_timestamps and now - self._call_timestamps[0] > self.window_seconds:
            self._call_timestamps.popleft()
            
        if len(self._call_timestamps) >= self.max_calls:
            logger.warning("Memory rate limit exceeded.")
            raise RateLimitExceeded(f"Rate limit of {self.max_calls} calls per {self.window_seconds}s exceeded.")
            
        self._call_timestamps.append(now)

    def store(self, entry: MemoryEntry) -> str:
        self._check_rate_limit()
        return self.backend.store(entry)

    def search(self, query: str, top_k: int = 5,
               category: Optional[str] = None,
               scope: Optional[MemoryScope] = None,
               scope_id: Optional[str] = None) -> List[SearchResult]:
        self._check_rate_limit()
        return self.backend.search(query, top_k, category, scope, scope_id)

    def get_by_category(self, category: str, top_k: int = 5,
                        success_filter: Optional[bool] = None) -> List[MemoryEntry]:
        self._check_rate_limit()
        return self.backend.get_by_category(category, top_k, success_filter)

    def count(self) -> int:
        return self.backend.count()

    def clear(self) -> None:
        self.backend.clear()

    def save(self, path: Optional[str] = None) -> None:
        self.backend.save(path)

    def load(self, path: Optional[str] = None) -> None:
        self.backend.load(path)

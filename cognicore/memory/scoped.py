import logging
from typing import List, Optional

from cognicore.memory.base import MemoryBackend, MemoryEntry, SearchResult, MemoryScope

logger = logging.getLogger(__name__)

class ScopedMemoryBackend(MemoryBackend):
    """
    A wrapper around a MemoryBackend that automatically filters by,
    and applies, a specific scope and scope_id.
    """

    def __init__(self, backend: MemoryBackend, scope: MemoryScope, scope_id: str):
        self.backend = backend
        self.scope = scope
        self.scope_id = scope_id

    def store(self, entry: MemoryEntry) -> str:
        # Override the entry's scope to match this scoped backend
        entry.scope = self.scope
        entry.scope_id = self.scope_id
        return self.backend.store(entry)

    def search(self, query: str, top_k: int = 5,
               category: Optional[str] = None,
               scope: Optional[MemoryScope] = None,
               scope_id: Optional[str] = None) -> List[SearchResult]:
        # Ignore passed scope, force the scoped backend's scope
        return self.backend.search(query, top_k, category, self.scope, self.scope_id)

    def get_by_category(self, category: str, top_k: int = 5,
                        success_filter: Optional[bool] = None) -> List[MemoryEntry]:
        # Filter get_by_category by checking scope manually if backend doesn't support scope filtering
        # directly in get_by_category (our backends currently don't take scope in get_by_category)
        # So we fetch more and filter manually here.
        all_entries = self.backend.get_by_category(category, top_k=top_k * 5, success_filter=success_filter)
        scoped_entries = [e for e in all_entries if e.scope == self.scope and e.scope_id == self.scope_id]
        return scoped_entries[:top_k]

    def count(self) -> int:
        # Note: this is a heuristic count since filtering the whole backend might be slow.
        # But we'll just do it anyway.
        # This is an optional feature.
        return len([1 for e in self.backend.get_by_category("general", top_k=999999) 
                    if e.scope == self.scope and e.scope_id == self.scope_id])

    def clear(self) -> None:
        # Clearing a scoped backend should ideally just clear that scope.
        # But since our backends don't have scoped clear, this is a no-op or raises.
        raise NotImplementedError("Cannot clear a scoped backend wrapper directly.")

    def save(self, path: Optional[str] = None) -> None:
        self.backend.save(path)

    def load(self, path: Optional[str] = None) -> None:
        self.backend.load(path)

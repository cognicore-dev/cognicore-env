import asyncio
import logging
from typing import List, Optional

from cognicore.memory.base import MemoryBackend, MemoryEntry, SearchResult, MemoryScope

logger = logging.getLogger(__name__)

class AsyncMemoryBackend:
    """
    Asynchronous wrapper for MemoryBackend implementations.
    Executes synchronous storage and retrieval operations in a thread pool
    to prevent blocking the async event loop.
    """

    def __init__(self, backend: MemoryBackend):
        self.backend = backend

    async def store(self, entry: MemoryEntry) -> str:
        return await asyncio.to_thread(self.backend.store, entry)

    async def search(self, query: str, top_k: int = 5,
                     category: Optional[str] = None,
                     scope: Optional[MemoryScope] = None,
                     scope_id: Optional[str] = None) -> List[SearchResult]:
        return await asyncio.to_thread(
            self.backend.search, query, top_k, category, scope, scope_id
        )

    async def get_by_category(self, category: str, top_k: int = 5,
                              success_filter: Optional[bool] = None) -> List[MemoryEntry]:
        return await asyncio.to_thread(
            self.backend.get_by_category, category, top_k, success_filter
        )

    async def count(self) -> int:
        return await asyncio.to_thread(self.backend.count)

    async def clear(self) -> None:
        await asyncio.to_thread(self.backend.clear)

    async def save(self, path: Optional[str] = None) -> None:
        await asyncio.to_thread(self.backend.save, path)

    async def load(self, path: Optional[str] = None) -> None:
        await asyncio.to_thread(self.backend.load, path)

import logging
import os
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any

from cognicore.memory import SQLiteMemoryBackend, MemoryEntry, MemoryScope
from cognicore.memory.decompose import decompose
from cognicore.memory.categorize import auto_categorize

logger = logging.getLogger("cognicore.extension")

# Optional: Semantic search dependency
try:
    from sentence_transformers import SentenceTransformer
    class SentenceTransformerProvider:
        def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
            self.model = SentenceTransformer(model_name)
        def embed(self, text: str) -> List[float]:
            return self.model.encode(text).tolist()
        def embed_batch(self, texts: List[str]) -> List[List[float]]:
            return self.model.encode(texts).tolist()
        @property
        def dimension(self) -> int:
            return self.model.get_sentence_embedding_dimension()
    _HAS_SEMANTIC = True
except ImportError:
    _HAS_SEMANTIC = False
    SentenceTransformerProvider = None

# ---------------------------------------------------------------------------
# MCP SDK import (optional dependency)
# ---------------------------------------------------------------------------
try:
    from mcp.server.fastmcp import FastMCP
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


_backend: Optional[SQLiteMemoryBackend] = None

def _get_data_dir() -> Path:
    raw = os.environ.get("COGNICORE_EXTENSION_DIR", "")
    if raw:
        data_dir = Path(raw)
    else:
        data_dir = Path.home() / ".cognicore" / "extension"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir

def _get_project_id() -> str:
    """Finds the canonical project ID by searching for .git root and hashing its absolute path."""
    import hashlib
    current = Path.cwd().resolve()
    target = current
    
    # Traverse up to find .git
    for p in [current] + list(current.parents):
        if (p / ".git").is_dir():
            target = p
            break
            
    # Create a stable, sanitized hash of the absolute path
    path_str = str(target)
    return hashlib.sha256(path_str.encode("utf-8")).hexdigest()

def _ensure_backend():
    global _backend
    if _backend is not None:
        return
    
    db_path = _get_data_dir() / "memory.db"
    
    provider = None
    if _HAS_SEMANTIC and os.environ.get("COGNICORE_USE_SEMANTIC", "0") == "1":
        try:
            provider = SentenceTransformerProvider()
            logger.info("Semantic search enabled via sentence-transformers")
        except Exception as e:
            logger.warning(f"Failed to load sentence-transformers, falling back to lexical search: {e}")
            
    _backend = SQLiteMemoryBackend(str(db_path), provider=provider)
    logger.info(f"Initialized CogniCore Extension Memory at {db_path}")


def create_extension_server() -> "FastMCP":
    if not _MCP_AVAILABLE:
        raise ImportError(
            "The 'mcp' package is required for MCP server support. "
            "Install it with: pip install mcp"
        )
        
    mcp = FastMCP(
        "cognicore-memory",
        instructions=(
            "Provides persistent, long-term memory for Claude Desktop. "
            "Store, recall, and manage user preferences, project details, and key facts."
        )
    )

    import time as _time
    _server_start = _time.time()

    def _normalize_score(score: float, max_score: float) -> float:
        if max_score <= 0:
            return 0.0
        return round(min(score / max_score, 1.0), 3)

    @mcp.tool()
    def cognicore_remember(text: str, category: str = "", scope: str = "user") -> str:
        """Store a fact, preference, or decision. Auto-decomposes and auto-categorizes."""
        _ensure_backend()
        
        try:
            mem_scope = MemoryScope(scope.lower())
        except ValueError:
            return "Error: scope must be 'user' or 'project'."
            
        scope_id = _get_project_id() if mem_scope == MemoryScope.PROJECT else ""
        
        facts = decompose(text)
        ids = []
        cats_detected = set()
        for fact in facts:
            if category:
                fact_cat = category
            else:
                fact_cat, _ = auto_categorize(fact)
            cats_detected.add(fact_cat)

            entry = MemoryEntry(
                text=fact,
                category=fact_cat,
                scope=mem_scope,
                scope_id=scope_id,
                memory_type="semantic"
            )
            try:
                ids.append(str(_backend.store(entry)))
            except Exception as e:
                logger.error(f"Failed to store fact: {e}")
                return f"Error: {e}"

        cat_str = ",".join(sorted(cats_detected))
        if len(ids) == 1:
            return f"Stored 1 fact (id={ids[0]}, cat={cat_str})"
        return f"Stored {len(ids)} facts (ids={','.join(ids)}, cats={cat_str})"

    @mcp.tool()
    def cognicore_recall(query: str, category: str = "", scope: str = "user", top_k: int = 5) -> str:
        """Search memory. Returns scored results sorted by relevance."""
        _ensure_backend()
        
        try:
            mem_scope = MemoryScope(scope.lower())
        except ValueError:
            return "Error: scope must be 'user' or 'project'."
            
        scope_id = _get_project_id() if mem_scope == MemoryScope.PROJECT else None
        
        try:
            results = _backend.search(
                query=query, 
                top_k=top_k, 
                category=category if category else None,
                scope=mem_scope,
                scope_id=scope_id
            )
            
            if not results:
                return "(none)"

            max_score = max(r.score for r in results) if results else 1.0
            max_score = max(max_score, 0.001)

            lines = [f"Found {len(results)} memories:"]
            for r in results:
                norm = _normalize_score(r.score, max_score)
                lines.append(f"  [{norm:.2f}] {r.entry.text} ({r.entry.category}) #{r.entry.entry_id}")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"Failed to recall memories: {e}")
            return f"Error: {e}"

    @mcp.tool()
    def cognicore_forget(entry_id: str) -> str:
        """Delete a memory by ID."""
        _ensure_backend()
        
        try:
            success = _backend.delete(entry_id)
            return "OK" if success else "Not found"
        except Exception as e:
            logger.error(f"Failed to delete memory: {e}")
            return f"Error: {e}"

    @mcp.tool()
    def cognicore_list(limit: int = 10, category: str = "", scope: str = "user") -> str:
        """List recent memories with categories."""
        _ensure_backend()
        
        try:
            mem_scope = MemoryScope(scope.lower())
        except ValueError:
            return "Error: scope must be 'user' or 'project'."
            
        scope_id = _get_project_id() if mem_scope == MemoryScope.PROJECT else None
        
        try:
            results = _backend.search(
                query="", 
                top_k=limit, 
                category=category if category else None,
                scope=mem_scope,
                scope_id=scope_id
            )
            
            if not results:
                return "(empty)"
            lines = []
            for r in results:
                lines.append(f"#{r.entry.entry_id}: {r.entry.text} ({r.entry.category})")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"Failed to list memories: {e}")
            return f"Error: {e}"

    @mcp.tool()
    def cognicore_stats() -> str:
        """Memory statistics: count, categories, uptime, storage."""
        _ensure_backend()
        
        try:
            count = _backend.count()
            db_path = _get_data_dir() / "memory.db"
            mode = "Semantic (sentence-transformers)" if _backend.provider else "BM25 (SQLite FTS5)"
            
            # Category breakdown
            all_results = _backend.search(query="", top_k=10000, scope=MemoryScope.USER)
            cat_counts = {}
            for r in all_results:
                cat = r.entry.category or "general"
                cat_counts[cat] = cat_counts.get(cat, 0) + 1

            uptime_s = int(_time.time() - _server_start)
            hours, remainder = divmod(uptime_s, 3600)
            minutes, seconds = divmod(remainder, 60)

            lines = [
                f"Total memories: {count}",
                f"Search mode: {mode}",
                f"Uptime: {hours}h {minutes}m {seconds}s",
                f"Storage: {db_path}",
                f"Categories:"
            ]
            for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {cat}: {cnt}")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return f"Error: {e}"

    return mcp

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    # stdio uses stdout for communication, so ensure logger goes to stderr
    logging.getLogger().handlers[0].stream = sys.stderr
    
    server = create_extension_server()
    server.run(transport="stdio")

if __name__ == "__main__":
    main()

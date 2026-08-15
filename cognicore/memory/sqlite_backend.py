import json
import logging
import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from cognicore.memory.base import MemoryBackend, MemoryEntry, MemoryState, SearchResult, MemoryScope, EmbeddingProvider
from cognicore.memory.events import event_bus

logger = logging.getLogger(__name__)

class SQLiteMemoryBackend(MemoryBackend):
    """
    SQLite-backed memory store with FTS5 for text search and optional
    in-python cosine similarity for embeddings.
    """

    def __init__(self, db_path: str, provider: Optional[EmbeddingProvider] = None):
        self.db_path = Path(db_path).expanduser()
        self.provider = provider
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_entries (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    category TEXT,
                    correct INTEGER,
                    action TEXT,
                    scope TEXT,
                    scope_id TEXT,
                    metadata_json TEXT,
                    embedding_json TEXT,
                    timestamp REAL,
                    relevance REAL,
                    state TEXT DEFAULT 'candidate',
                    memory_type TEXT DEFAULT 'semantic',
                    importance REAL DEFAULT 0.5,
                    utility_score REAL DEFAULT 0.0,
                    retrieval_count INTEGER DEFAULT 0,
                    used_count INTEGER DEFAULT 0,
                    ignored_count INTEGER DEFAULT 0,
                    positive_outcomes INTEGER DEFAULT 0,
                    negative_outcomes INTEGER DEFAULT 0,
                    last_accessed REAL DEFAULT 0.0,
                    creation_reason TEXT DEFAULT '',
                    source_component TEXT DEFAULT '',
                    source_agent TEXT DEFAULT '',
                    source_task TEXT DEFAULT '',
                    confidence REAL DEFAULT 1.0,
                    session_id TEXT DEFAULT 'default',
                    sequence_id INTEGER DEFAULT 0,
                    supersedes TEXT,
                    invalidated_by TEXT,
                    invalidated_reason TEXT
                )
            """)
            # Auto-migrate: add new columns to existing databases
            self._migrate_schema(conn)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    text,
                    content='memory_entries',
                    content_rowid='entry_id'
                )
            """)
            # Triggers to keep FTS synchronized
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_entries_ai AFTER INSERT ON memory_entries BEGIN
                    INSERT INTO memory_fts(rowid, text) VALUES (new.entry_id, new.text);
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_entries_ad AFTER DELETE ON memory_entries BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES('delete', old.entry_id, old.text);
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_entries_au AFTER UPDATE ON memory_entries BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES('delete', old.entry_id, old.text);
                    INSERT INTO memory_fts(rowid, text) VALUES (new.entry_id, new.text);
                END;
            """)

    def _migrate_schema(self, conn):
        """Auto-add v2 lifecycle columns and session tables to existing databases."""
        cursor = conn.execute("PRAGMA table_info(memory_entries)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        migrations = [
            ("state", "TEXT DEFAULT 'candidate'"),
            ("memory_type", "TEXT DEFAULT 'semantic'"),
            ("importance", "REAL DEFAULT 0.5"),
            ("utility_score", "REAL DEFAULT 0.0"),
            ("retrieval_count", "INTEGER DEFAULT 0"),
            ("used_count", "INTEGER DEFAULT 0"),
            ("ignored_count", "INTEGER DEFAULT 0"),
            ("positive_outcomes", "INTEGER DEFAULT 0"),
            ("negative_outcomes", "INTEGER DEFAULT 0"),
            ("last_accessed", "REAL DEFAULT 0.0"),
            ("creation_reason", "TEXT DEFAULT ''"),
            ("source_component", "TEXT DEFAULT ''"),
            ("source_agent", "TEXT DEFAULT ''"),
            ("source_task", "TEXT DEFAULT ''"),
            ("confidence", "REAL DEFAULT 1.0"),
            ("session_id", "TEXT DEFAULT 'default'"),
            ("sequence_id", "INTEGER DEFAULT 0"),
            ("supersedes", "TEXT"),
            ("invalidated_by", "TEXT"),
            ("invalidated_reason", "TEXT"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE memory_entries ADD COLUMN {col_name} {col_def}")
                except Exception as e:
                    logger.warning(f"Migration error for {col_name}: {e}")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                name TEXT,
                timestamp REAL,
                message_count INTEGER,
                total_tokens_estimated INTEGER,
                summary_memory_id INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_memories (
                session_id TEXT,
                memory_id INTEGER,
                PRIMARY KEY (session_id, memory_id)
            )
        """)

    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        scope_str = row["scope"]
        try:
            scope = MemoryScope(scope_str)
        except ValueError:
            scope = MemoryScope.GLOBAL

        metadata = {}
        if row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
            except Exception:
                pass

        return MemoryEntry(
            text=row["text"],
            category=row["category"] or "general",
            correct=bool(row["correct"]) if row["correct"] is not None else None,
            action=row["action"] or "",
            scope=scope,
            scope_id=row["scope_id"] or "",
            metadata=metadata,
            entry_id=str(row["entry_id"]),
            timestamp=row["timestamp"] or 0.0,
            relevance=row["relevance"] or 1.0,
            # v2 fields
            state=row["state"] if "state" in row.keys() else "candidate",
            memory_type=row["memory_type"] if "memory_type" in row.keys() else "semantic",
            importance=row["importance"] if "importance" in row.keys() else 0.5,
            utility_score=row["utility_score"] if "utility_score" in row.keys() else 0.0,
            retrieval_count=row["retrieval_count"] if "retrieval_count" in row.keys() else 0,
            used_count=row["used_count"] if "used_count" in row.keys() else 0,
            ignored_count=row["ignored_count"] if "ignored_count" in row.keys() else 0,
            positive_outcomes=row["positive_outcomes"] if "positive_outcomes" in row.keys() else 0,
            negative_outcomes=row["negative_outcomes"] if "negative_outcomes" in row.keys() else 0,
            last_accessed=row["last_accessed"] if "last_accessed" in row.keys() else 0.0,
            creation_reason=row["creation_reason"] if "creation_reason" in row.keys() else "",
            source_component=row["source_component"] if "source_component" in row.keys() else "",
            source_agent=row["source_agent"] if "source_agent" in row.keys() else "",
            source_task=row["source_task"] if "source_task" in row.keys() else "",
            confidence=row["confidence"] if "confidence" in row.keys() else 1.0,
            session_id=row["session_id"] if "session_id" in row.keys() else "default",
            sequence_id=row["sequence_id"] if "sequence_id" in row.keys() else 0,
            supersedes=row["supersedes"] if "supersedes" in row.keys() else None,
            invalidated_by=row["invalidated_by"] if "invalidated_by" in row.keys() else None,
            invalidated_reason=row["invalidated_reason"] if "invalidated_reason" in row.keys() else "",
        )

    def store(self, entry: MemoryEntry) -> str:
        embedding_json = None
        if self.provider:
            text_to_embed = entry.text or entry.action
            vec = self.provider.embed(text_to_embed)
            # normalize for cosine sim
            norm = sum(x*x for x in vec) ** 0.5
            if norm > 0:
                vec = [x/norm for x in vec]
            embedding_json = json.dumps(vec)

        metadata_json = json.dumps(entry.metadata) if entry.metadata else None
        timestamp = entry.timestamp or time.time()
        last_accessed = entry.last_accessed or timestamp

        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO memory_entries (
                    text, category, correct, action, scope, scope_id,
                    metadata_json, embedding_json, timestamp, relevance,
                    state, memory_type, importance, utility_score,
                    retrieval_count, used_count, ignored_count,
                    positive_outcomes, negative_outcomes, last_accessed,
                    creation_reason, source_component, source_agent, source_task,
                    confidence, session_id, sequence_id, supersedes,
                    invalidated_by, invalidated_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.text,
                entry.category,
                1 if entry.correct else 0 if entry.correct is False else None,
                entry.action,
                entry.scope.value,
                entry.scope_id,
                metadata_json,
                embedding_json,
                timestamp,
                entry.relevance,
                entry.state,
                entry.memory_type,
                entry.importance,
                entry.utility_score,
                entry.retrieval_count,
                entry.used_count,
                entry.ignored_count,
                entry.positive_outcomes,
                entry.negative_outcomes,
                last_accessed,
                entry.creation_reason,
                entry.source_component,
                entry.source_agent,
                entry.source_task,
                entry.confidence,
                entry.session_id,
                entry.sequence_id,
                entry.supersedes,
                entry.invalidated_by,
                entry.invalidated_reason,
            ))
            entry_id = str(cursor.lastrowid)
            entry.entry_id = entry_id
            
            event_bus.publish("on_store", entry=entry, entry_id=entry_id)
            return entry_id

    def search(self, query: str, top_k: int = 5,
               category: Optional[str] = None,
               scope: Optional[MemoryScope] = None,
               scope_id: Optional[str] = None,
               question_timestamp: Optional[float] = None,
               metadata_filters: Optional[Dict[str, str]] = None) -> List[SearchResult]:
        
        # If we have an embedding provider, we use it for semantic search + BM25 score
        query_vec = None
        if self.provider and query.strip():
            try:
                query_vec = self.provider.embed(query)
                norm = sum(x*x for x in query_vec) ** 0.5
                if norm > 0:
                    query_vec = [x/norm for x in query_vec]
            except Exception as e:
                logger.warning(f"Failed to embed query: {e}")
                query_vec = None

        with self._get_conn() as conn:
            if query_vec:
                # Semantic / Hybrid search mode: evaluate across candidate corpus
                sql = "SELECT *, 0 as bm25_score FROM memory_entries WHERE 1=1"
                params = []
                if category:
                    sql += " AND category = ?"
                    params.append(category)
                if scope:
                    sql += " AND scope = ?"
                    params.append(scope.value)
                if scope_id:
                    sql += " AND scope_id = ?"
                    params.append(scope_id)
                sql += " ORDER BY timestamp DESC LIMIT 5000"
                rows = conn.execute(sql, params).fetchall()

                if metadata_filters:
                    filtered_rows = []
                    for row in rows:
                        try:
                            meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
                            if all(meta.get(k) == v for k, v in metadata_filters.items()):
                                filtered_rows.append(row)
                        except Exception:
                            pass
                    rows = filtered_rows

                if not rows:
                    return []

                # Calculate BM25 scores across candidate set for normalization
                bm25_res = self._bm25_search(rows, query, len(rows), category, scope, scope_id, metadata_filters)
                bm25_map = {r.entry.entry_id: r.score for r in bm25_res}
                max_bm25 = max(bm25_map.values()) if bm25_map else 1.0
                max_bm25 = max(max_bm25, 0.001)

                results = []
                for row in rows:
                    entry = self._row_to_entry(row)
                    sim_score = 0.0
                    if row["embedding_json"]:
                        try:
                            vec = json.loads(row["embedding_json"])
                            if len(vec) == len(query_vec):
                                sim_score = sum(a*b for a, b in zip(query_vec, vec))
                        except Exception:
                            pass
                    elif self.provider:
                        # On-the-fly embed and save if missing
                        try:
                            text_to_embed = row["text"] or row["action"] or ""
                            if text_to_embed:
                                vec = self.provider.embed(text_to_embed)
                                norm = sum(x*x for x in vec) ** 0.5
                                if norm > 0:
                                    vec = [x/norm for x in vec]
                                sim_score = sum(a*b for a, b in zip(query_vec, vec))
                                conn.execute("UPDATE memory_entries SET embedding_json = ? WHERE entry_id = ?", 
                                             (json.dumps(vec), row["entry_id"]))
                        except Exception:
                            pass
                    
                    sim_score = max(0.0, sim_score)
                    norm_bm25 = bm25_map.get(entry.entry_id, 0.0) / max_bm25
                    final_score = 0.7 * sim_score + 0.3 * norm_bm25

                    if final_score > 0.0:
                        results.append(SearchResult(entry=entry, score=round(final_score, 4), source="hybrid"))

                results.sort(key=lambda x: x.score, reverse=True)
                final_results = results[:top_k]
                event_bus.publish("on_search", query=query, top_k=top_k, results=final_results)
                return final_results

            # Lexical / BM25 mode: FTS5 prefix search with full-corpus BM25 fallback
            rows = []
            if query.strip():
                clean_query = query.replace('"', '').replace("'", '').strip()
                fts_tokens = ' '.join(f'{w}*' for w in clean_query.split() if w)
                
                fts_sql = """
                    SELECT rowid, rank as bm25_score
                    FROM memory_fts
                    WHERE memory_fts MATCH ?
                """
                sql = f"""
                    SELECT m.*, COALESCE(f.bm25_score, 0) as bm25_score
                    FROM memory_entries m
                    INNER JOIN ({fts_sql}) f ON m.entry_id = f.rowid
                    WHERE 1=1
                """  # nosec B608
                params = [fts_tokens]
            
                if category:
                    sql += " AND m.category = ?"
                    params.append(category)
                if scope:
                    sql += " AND m.scope = ?"
                    params.append(scope.value)
                if scope_id:
                    sql += " AND m.scope_id = ?"
                    params.append(scope_id)

                try:
                    cursor = conn.execute(sql, params)
                    rows = cursor.fetchall()
                except Exception as e:
                    logger.warning(f"FTS5 search failed: {e}")
                    rows = []

                if not rows:
                    logger.info("FTS5 returned no results; running BM25 fallback")
                    return self._fallback_search(
                        conn, query, top_k, category, scope, scope_id, metadata_filters
                    )
            else:
                sql = """
                    SELECT m.*, 0 as bm25_score
                    FROM memory_entries m
                    WHERE 1=1
                """
                params = []

                if category:
                    sql += " AND m.category = ?"
                    params.append(category)
                if scope:
                    sql += " AND m.scope = ?"
                    params.append(scope.value)
                if scope_id:
                    sql += " AND m.scope_id = ?"
                    params.append(scope_id)

                sql += " ORDER BY timestamp DESC LIMIT 1000"
                cursor = conn.execute(sql, params)
                rows = cursor.fetchall()
            
            results = []
            for row in rows:
                if metadata_filters:
                    try:
                        meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
                        if not all(meta.get(k) == v for k, v in metadata_filters.items()):
                            continue
                    except Exception:
                        continue
                        
                entry = self._row_to_entry(row)
                bm25_score = -row["bm25_score"]
                results.append(SearchResult(entry=entry, score=bm25_score, source="sqlite"))
            
            results.sort(key=lambda x: x.score, reverse=True)
            final_results = results[:top_k]
            event_bus.publish("on_search", query=query, top_k=top_k, results=final_results)
            return final_results

    # ------------------------------------------------------------------
    # BM25 Semantic Search (zero extra deps, uses stdlib math only)
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Simple whitespace + punctuation tokenizer (mirrors TFIDFMemoryBackend)."""
        if not text:
            return []
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        return [t for t in text.split() if len(t) > 1]

    def _bm25_search(self, rows: list, query: str, top_k: int,
                     category: Optional[str], scope: Optional[MemoryScope],
                     scope_id: Optional[str],
                     metadata_filters: Optional[Dict[str, str]] = None) -> List[SearchResult]:
        """BM25 Okapi ranking over a list of sqlite3.Row objects.
        
        This gives proper term-importance scoring so natural-language queries
        like "who built CogniCore" correctly rank memories mentioning
        "built" and "CogniCore" even when stop-words differ.
        """
        if not rows:
            return []

        q_tokens = self._tokenize(query)
        if not q_tokens:
            return []

        # Build per-document token lists
        texts = [row["text"] or "" for row in rows]
        tokenized = [self._tokenize(t) for t in texts]

        # Document frequency for IDF
        N = len(tokenized)
        df: Dict[str, int] = defaultdict(int)
        for toks in tokenized:
            for tok in set(toks):
                df[tok] += 1

        # BM25 hyper-parameters (standard values)
        k1, b = 1.5, 0.75
        avgdl = sum(len(t) for t in tokenized) / N if N else 1

        scores = []
        for idx, toks in enumerate(tokenized):
            row = rows[idx]
            # Apply hard filters
            if category and (row["category"] or "") != category:
                continue
            if scope and (row["scope"] or "") != scope.value:
                continue
            if scope_id and (row["scope_id"] or "") != scope_id:
                continue
                
            if metadata_filters:
                try:
                    meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
                    if not all(meta.get(k) == v for k, v in metadata_filters.items()):
                        continue
                except Exception:
                    continue

            dl = len(toks) or 1
            tf_map = Counter(toks)
            score = 0.0
            for tok in q_tokens:
                if tok not in tf_map:
                    continue
                idf = math.log((N - df[tok] + 0.5) / (df[tok] + 0.5) + 1)
                tf = tf_map[tok]
                score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
            scores.append((score, idx))

        scores.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, idx in scores[:top_k]:
            if score <= 0:
                break
            entry = self._row_to_entry(rows[idx])
            results.append(SearchResult(entry=entry, score=score, source="bm25"))
        return results

    def _fallback_search(self, conn, query: str, top_k: int,
                         category: Optional[str], scope: Optional[MemoryScope],
                         scope_id: Optional[str],
                         metadata_filters: Optional[Dict[str, str]] = None) -> List[SearchResult]:
        """Load all rows and run BM25; fall back to LIKE only for empty corpora."""
        all_sql = "SELECT *, 0 as bm25_score FROM memory_entries ORDER BY timestamp DESC"
        all_rows = conn.execute(all_sql).fetchall()

        if not all_rows:
            return []

        results = self._bm25_search(all_rows, query, top_k, category, scope, scope_id, metadata_filters)
        if results:
            logger.info("BM25 fallback found %d results", len(results))
            return results

        # Last resort: LIKE scan (handles very short corpora where BM25 scores all 0)
        logger.info("BM25 scored 0; falling back to LIKE scan")
        clean_query = query.replace('"', '').replace("'", '').strip()
        like_sql = "SELECT *, 0 as bm25_score FROM memory_entries WHERE text LIKE ?"
        like_params: List = [f"%{clean_query}%"]
        if category:
            like_sql += " AND category = ?"
            like_params.append(category)
        if scope:
            like_sql += " AND scope = ?"
            like_params.append(scope.value)
        if scope_id:
            like_sql += " AND scope_id = ?"
            like_params.append(scope_id)
        like_sql += " ORDER BY timestamp DESC LIMIT 1000"
        like_rows = conn.execute(like_sql, like_params).fetchall()  # nosec B608
        
        if metadata_filters:
            filtered_like_rows = []
            for row in like_rows:
                try:
                    meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
                    if all(meta.get(k) == v for k, v in metadata_filters.items()):
                        filtered_like_rows.append(row)
                except Exception:
                    pass
            like_rows = filtered_like_rows

        return [
            SearchResult(entry=self._row_to_entry(r), score=1.0, source="like")
            for r in like_rows[:top_k]
        ]

    def get_by_category(self, category: str, top_k: int = 5,
                        success_filter: Optional[bool] = None) -> List[MemoryEntry]:
        sql = "SELECT * FROM memory_entries WHERE category = ?"
        params = [category]
        if success_filter is not None:
            sql += " AND correct = ?"
            params.append(1 if success_filter else 0)
        
        sql += " ORDER BY entry_id DESC LIMIT ?"
        params.append(top_k)

        with self._get_conn() as conn:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            return [self._row_to_entry(row) for row in rows]

    def count(self) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM memory_entries WHERE state NOT IN ('archived', 'deleted')")
            return cursor.fetchone()[0]

    def clear(self) -> None:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM memory_entries")

    # ------------------------------------------------------------------
    # v2 Lifecycle Methods
    # ------------------------------------------------------------------

    def update(self, entry_id: str, **fields) -> bool:
        """Update fields on an existing entry via SQL UPDATE."""
        allowed = {
            'state', 'importance', 'utility_score', 'retrieval_count',
            'used_count', 'ignored_count', 'positive_outcomes', 'negative_outcomes',
            'last_accessed', 'relevance', 'confidence', 'memory_type',
            'creation_reason', 'source_component', 'source_agent', 'source_task',
            'supersedes', 'invalidated_by', 'invalidated_reason'
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False

        set_clause = ', '.join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [entry_id]
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"UPDATE memory_entries SET {set_clause} WHERE entry_id = ?",  # nosec B608
                values
            )
            return cursor.rowcount > 0

    def get_by_id(self, entry_id: str):
        """Retrieve a single entry by its ID."""
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT * FROM memory_entries WHERE entry_id = ?", (entry_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_entry(row)
        return None

    def get_by_state(self, state: str, limit: int = 100):
        """Retrieve entries in a given lifecycle state."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM memory_entries WHERE state = ? LIMIT ?",
                (state, limit)
            )
            return [self._row_to_entry(row) for row in cursor.fetchall()]

    def get_by_type(self, memory_type: str, limit: int = 100):
        """Retrieve entries of a given memory type."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM memory_entries WHERE memory_type = ? AND state NOT IN ('archived', 'deleted') LIMIT ?",
                (memory_type, limit)
            )
            return [self._row_to_entry(row) for row in cursor.fetchall()]

    def get_all(self, limit: int = 10000):
        """Retrieve all active entries."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM memory_entries WHERE state NOT IN ('deleted') ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            )
            return [self._row_to_entry(row) for row in cursor.fetchall()]

    def delete(self, entry_id: str) -> bool:
        """Hard-delete an entry."""
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM memory_entries WHERE entry_id = ?", (entry_id,))
            return cursor.rowcount > 0

    def _get_all_entries(self):
        return self.get_all()

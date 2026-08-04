"""
Marketplace and Commerce engine for CogniCore.
"""

import json
import sqlite3
import hashlib
import time
import math
import logging
from datetime import datetime
from typing import List, Dict, Optional, Any
from collections import defaultdict

logger = logging.getLogger('cognicore.commerce')

class CommerceDB:
    """Base class for commerce database operations."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Returns sqlite3 connection with WAL mode and 10s timeout."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        """Creates the necessary schemas."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_registry (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    reputation_score REAL DEFAULT 0.5,
                    total_memories INTEGER DEFAULT 0,
                    categories TEXT,
                    for_sale BOOLEAN DEFAULT 0,
                    registered_at TEXT,
                    last_active TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    seller_agent_id TEXT,
                    buyer_agent_id TEXT,
                    memory_ids TEXT,
                    memory_type TEXT,
                    memories_count INTEGER,
                    price_usd REAL,
                    timestamp TEXT,
                    hash TEXT,
                    seller_reputation_before REAL,
                    buyer_reputation_before REAL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_prices (
                    agent_id TEXT,
                    category TEXT,
                    memory_type TEXT,
                    price_per_unit REAL,
                    PRIMARY KEY (agent_id, category, memory_type)
                );
            """)

class AgentRegistry(CommerceDB):
    """Manages agent registrations in the marketplace."""
    def register(self, agent_id: str, name: str = '', description: str = '', categories: Optional[List[str]] = None):
        """Registers or updates an agent."""
        now = datetime.utcnow().isoformat()
        cats_json = json.dumps(categories or [])
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO agent_registry (agent_id, name, description, categories, registered_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    categories=excluded.categories,
                    last_active=excluded.last_active
            """, (agent_id, name, description, cats_json, now, now))
    
    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Gets an agent's details."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM agent_registry WHERE agent_id = ?", (agent_id,)).fetchone()
            return dict(row) if row else None

    def update_stats(self, agent_id: str, total_memories: int, categories: List[str]):
        """Updates an agent's stats and categories."""
        now = datetime.utcnow().isoformat()
        cats_json = json.dumps(categories)
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE agent_registry 
                SET total_memories = ?, categories = ?, last_active = ?
                WHERE agent_id = ?
            """, (total_memories, cats_json, now, agent_id))

    def set_for_sale(self, agent_id: str, for_sale: bool = True):
        """Sets the for_sale flag."""
        with self._get_conn() as conn:
            conn.execute("UPDATE agent_registry SET for_sale = ? WHERE agent_id = ?", (int(for_sale), agent_id))

    def search_sellers(self, query: str = '', min_reputation: float = 0.7, category: Optional[str] = None, top_k: int = 10) -> List[Dict[str, Any]]:
        """Searches for agents selling memories."""
        sql = "SELECT * FROM agent_registry WHERE for_sale = 1 AND reputation_score >= ?"
        params: List[Any] = [min_reputation]
        
        if query:
            sql += " AND (name LIKE ? OR description LIKE ?)"
            params.extend([f"%{query}%", f"%{query}%"])
            
        if category:
            sql += " AND categories LIKE ?"
            params.append(f'%"{category}"%')
            
        sql += " ORDER BY reputation_score DESC LIMIT ?"
        params.append(top_k)
        
        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

class TransactionLedger(CommerceDB):
    """Manages the transaction ledger."""
    def record(self, seller_id: str, buyer_id: str, memory_ids: List[str], memory_type: str, count: int, price_usd: float, seller_rep: float, buyer_rep: float) -> str:
        """Records a new transaction."""
        timestamp = datetime.utcnow().isoformat()
        m_ids_json = json.dumps(memory_ids)
        
        raw_str = f"{seller_id}:{buyer_id}:{m_ids_json}:{timestamp}"
        tx_hash = hashlib.sha256(raw_str.encode('utf-8')).hexdigest()
        
        dt_str = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        tx_id = f"txn_{dt_str}_{tx_hash[:6]}"
        
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO transactions (
                    id, seller_agent_id, buyer_agent_id, memory_ids, memory_type, 
                    memories_count, price_usd, timestamp, hash, 
                    seller_reputation_before, buyer_reputation_before
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tx_id, seller_id, buyer_id, m_ids_json, memory_type, count, price_usd, timestamp, tx_hash, seller_rep, buyer_rep))
            
        return tx_id

    def get_transactions(self, agent_id: str, role: str = 'any') -> List[Dict[str, Any]]:
        """Gets transactions for an agent by role."""
        sql = "SELECT * FROM transactions WHERE "
        params: List[Any] = []
        if role == 'seller':
            sql += "seller_agent_id = ?"
            params.append(agent_id)
        elif role == 'buyer':
            sql += "buyer_agent_id = ?"
            params.append(agent_id)
        else:
            sql += "(seller_agent_id = ? OR buyer_agent_id = ?)"
            params.extend([agent_id, agent_id])
            
        sql += " ORDER BY timestamp DESC"
        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def get_stats(self, agent_id: str) -> Dict[str, Any]:
        """Gets transaction stats for an agent."""
        with self._get_conn() as conn:
            seller_rows = conn.execute("SELECT price_usd FROM transactions WHERE seller_agent_id = ?", (agent_id,)).fetchall()
            buyer_rows = conn.execute("SELECT price_usd FROM transactions WHERE buyer_agent_id = ?", (agent_id,)).fetchall()
            
        return {
            "total_as_seller": len(seller_rows),
            "total_as_buyer": len(buyer_rows),
            "total_revenue": sum(r['price_usd'] for r in seller_rows),
            "total_spent": sum(r['price_usd'] for r in buyer_rows)
        }

class ReputationEngine(CommerceDB):
    """Calculates and updates agent reputation."""
    def calculate(self, agent_id: str) -> float:
        """Calculates reputation score 0.0-1.0."""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT price_usd, memories_count FROM transactions WHERE seller_agent_id = ?", (agent_id,)).fetchall()
            
        count = len(rows)
        if count == 0:
            return 0.5
            
        base = min(0.3, math.log10(1 + count) * 0.1)
        
        total_memories = sum(r['memories_count'] for r in rows)
        total_revenue = sum(r['price_usd'] for r in rows)
        avg_price = total_revenue / total_memories if total_memories > 0 else 0
        quality = min(0.4, avg_price * 40)
        
        consistency = 0.0
        if count > 20:
            consistency = 0.3
        elif count > 5:
            consistency = 0.2
            
        total = base + quality + consistency
        return max(0.0, min(1.0, total))

    def update(self, agent_id: str):
        """Updates reputation in registry."""
        score = self.calculate(agent_id)
        with self._get_conn() as conn:
            conn.execute("UPDATE agent_registry SET reputation_score = ? WHERE agent_id = ?", (score, agent_id))

    def get(self, agent_id: str) -> Dict[str, Any]:
        """Gets reputation details."""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT price_usd, memories_count FROM transactions WHERE seller_agent_id = ?", (agent_id,)).fetchall()
            
        count = len(rows)
        score = self.calculate(agent_id)
        
        base_comp = min(0.3, math.log10(1 + count) * 0.1) if count > 0 else 0
        
        total_memories = sum(r['memories_count'] for r in rows)
        total_revenue = sum(r['price_usd'] for r in rows)
        avg_price = total_revenue / total_memories if total_memories > 0 else 0
        quality_comp = min(0.4, avg_price * 40) if count > 0 else 0
        
        consistency_comp = 0.3 if count > 20 else (0.2 if count > 5 else 0.0)
        
        return {
            "score": score,
            "total_transactions": count,
            "breakdown": {
                "base": base_comp,
                "quality": quality_comp,
                "consistency": consistency_comp
            }
        }

class PricingEngine:
    """Calculates memory prices."""
    BASE_PRICES = {'episodic': 0.0001, 'semantic': 0.01, 'procedural': 1.00}
    REPUTATION_MULTIPLIERS = [(0.95, 2.0), (0.90, 1.5), (0.80, 1.0), (0.70, 0.5)]
    RARITY_TIERS = {'common': 1.0, 'uncommon': 1.5, 'rare': 3.0}
    COMMON_CATEGORIES = {'general', 'null_handling', 'string_ops', 'basic_math', 'io_operations'}
    RARE_CATEGORIES = {'quantum_computing', 'formal_verification', 'cryptography', 'compiler_design'}

    def get_rarity(self, category: str) -> str:
        """Returns rarity tier for a category."""
        if category in self.RARE_CATEGORIES:
            return 'rare'
        if category in self.COMMON_CATEGORIES:
            return 'common'
        return 'uncommon'

    def price_memory(self, memory_type: str, confidence: float, reputation: float, category: str) -> float:
        """Calculates price for a single memory."""
        base = self.BASE_PRICES.get(memory_type, 0.01)
        
        rep_mult = 0.5
        for threshold, mult in self.REPUTATION_MULTIPLIERS:
            if reputation >= threshold:
                rep_mult = mult
                break
                
        rarity = self.get_rarity(category)
        rarity_mult = self.RARITY_TIERS.get(rarity, 1.0)
        
        return base * rep_mult * rarity_mult * confidence

    def value_collection(self, memories: List[Dict[str, Any]], reputation: float) -> Dict[str, Any]:
        """Values a collection of memories."""
        total = 0.0
        breakdown: Dict[str, float] = defaultdict(float)
        
        for m in memories:
            m_type = m.get('memory_type', 'semantic')
            conf = m.get('confidence', 1.0)
            cat = m.get('category', 'general')
            
            val = self.price_memory(m_type, conf, reputation, cat)
            total += val
            breakdown[m_type] += val
            
        return {
            "breakdown_by_type": dict(breakdown),
            "total_value_usd": total
        }

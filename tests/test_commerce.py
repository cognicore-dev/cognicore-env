"""
Tests for CogniCore Memory Commerce — marketplace, transfer, and pricing.
All tests use in-memory SQLite databases. Zero LLM API calls.
"""

import json
import os
import sys
import tempfile
import sqlite3
import pytest
from pathlib import Path

# Ensure project root is on path
project_root = str(Path(__file__).parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from cognicore.commerce.marketplace import (
    CommerceDB,
    AgentRegistry,
    TransactionLedger,
    ReputationEngine,
    PricingEngine,
)
from cognicore.commerce.transfer import MemoryTransfer
from cognicore.memory.base import MemoryEntry, MemoryScope


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def tmp_commerce_db(tmp_path):
    """Returns path to a temporary commerce database."""
    return str(tmp_path / "commerce_test.db")


@pytest.fixture
def tmp_memory_db(tmp_path):
    """Returns path to a temporary memory database."""
    return str(tmp_path / "memory_test.db")


@pytest.fixture
def registry(tmp_commerce_db):
    return AgentRegistry(tmp_commerce_db)


@pytest.fixture
def ledger(tmp_commerce_db):
    return TransactionLedger(tmp_commerce_db)


@pytest.fixture
def reputation(tmp_commerce_db):
    return ReputationEngine(tmp_commerce_db)


@pytest.fixture
def pricing():
    return PricingEngine()


class MockSearchResult:
    """Mimics SearchResult from SQLiteMemoryBackend."""
    def __init__(self, entry, score=1.0):
        self.entry = entry
        self.score = score
        self.source = "mock"


class MockBackend:
    """Minimal mock of SQLiteMemoryBackend for testing MemoryTransfer."""
    def __init__(self):
        self._memories = {}
        self._next_id = 1

    def store(self, entry: MemoryEntry) -> str:
        entry_id = str(self._next_id)
        entry.entry_id = entry_id
        self._memories[entry_id] = entry
        self._next_id += 1
        return entry_id

    def search(self, query="", top_k=5, scope=None, **kwargs):
        results = []
        for entry in list(self._memories.values())[:top_k]:
            results.append(MockSearchResult(entry))
        return results

    def get(self, entry_id: str):
        return self._memories.get(entry_id)

    def delete(self, entry_id: str) -> bool:
        if entry_id in self._memories:
            del self._memories[entry_id]
            return True
        return False


def _make_entry(text, category="general", memory_type="semantic", confidence=0.9, entry_id=""):
    """Helper to create a MemoryEntry."""
    return MemoryEntry(
        text=text,
        category=category,
        memory_type=memory_type,
        confidence=confidence,
        entry_id=entry_id,
        scope=MemoryScope.USER,
    )


# ═══════════════════════════════════════════════════════════
# AgentRegistry Tests
# ═══════════════════════════════════════════════════════════

class TestAgentRegistry:
    def test_register_and_get(self, registry):
        registry.register("agent_A", name="Django Agent", description="Backend expert", categories=["django", "python"])
        info = registry.get("agent_A")
        assert info is not None
        assert info["name"] == "Django Agent"
        assert info["description"] == "Backend expert"
        assert json.loads(info["categories"]) == ["django", "python"]

    def test_get_nonexistent(self, registry):
        assert registry.get("nonexistent") is None

    def test_register_updates_on_conflict(self, registry):
        registry.register("agent_A", name="V1")
        registry.register("agent_A", name="V2")
        info = registry.get("agent_A")
        assert info["name"] == "V2"

    def test_set_for_sale(self, registry):
        registry.register("agent_A")
        registry.set_for_sale("agent_A", True)
        info = registry.get("agent_A")
        assert info["for_sale"] == 1

    def test_search_sellers(self, registry):
        registry.register("seller_1", name="Python Expert", description="10 years Python", categories=["python"])
        registry.set_for_sale("seller_1", True)
        # Boost reputation to pass min_reputation filter
        with registry._get_conn() as conn:
            conn.execute("UPDATE agent_registry SET reputation_score = 0.9 WHERE agent_id = 'seller_1'")

        registry.register("seller_2", name="JS Expert", categories=["javascript"])
        registry.set_for_sale("seller_2", True)
        with registry._get_conn() as conn:
            conn.execute("UPDATE agent_registry SET reputation_score = 0.4 WHERE agent_id = 'seller_2'")

        # Should only find seller_1 (reputation >= 0.7)
        results = registry.search_sellers(min_reputation=0.7)
        assert len(results) == 1
        assert results[0]["agent_id"] == "seller_1"

    def test_search_sellers_by_category(self, registry):
        registry.register("s1", name="A", categories=["python", "django"])
        registry.set_for_sale("s1", True)
        with registry._get_conn() as conn:
            conn.execute("UPDATE agent_registry SET reputation_score = 0.9 WHERE agent_id = 's1'")

        registry.register("s2", name="B", categories=["javascript"])
        registry.set_for_sale("s2", True)
        with registry._get_conn() as conn:
            conn.execute("UPDATE agent_registry SET reputation_score = 0.9 WHERE agent_id = 's2'")

        results = registry.search_sellers(category="python", min_reputation=0.5)
        assert len(results) == 1
        assert results[0]["agent_id"] == "s1"

    def test_update_stats(self, registry):
        registry.register("agent_A")
        registry.update_stats("agent_A", total_memories=500, categories=["python", "ml"])
        info = registry.get("agent_A")
        assert info["total_memories"] == 500
        assert json.loads(info["categories"]) == ["python", "ml"]


# ═══════════════════════════════════════════════════════════
# TransactionLedger Tests
# ═══════════════════════════════════════════════════════════

class TestTransactionLedger:
    def test_record_and_retrieve(self, ledger):
        txn_id = ledger.record(
            seller_id="seller_A",
            buyer_id="buyer_B",
            memory_ids=["1", "2", "3"],
            memory_type="semantic",
            count=3,
            price_usd=0.03,
            seller_rep=0.8,
            buyer_rep=0.5,
        )
        assert txn_id.startswith("txn_")
        txns = ledger.get_transactions("seller_A", role="seller")
        assert len(txns) == 1
        assert txns[0]["seller_agent_id"] == "seller_A"

    def test_hash_is_sha256(self, ledger):
        txn_id = ledger.record("s", "b", ["1"], "semantic", 1, 0.01, 0.5, 0.5)
        txns = ledger.get_transactions("s")
        assert len(txns[0]["hash"]) == 64  # SHA256 hex digest

    def test_get_stats(self, ledger):
        ledger.record("seller", "buyer", ["1"], "semantic", 1, 0.10, 0.5, 0.5)
        ledger.record("seller", "buyer", ["2", "3"], "episodic", 2, 0.05, 0.5, 0.5)
        stats = ledger.get_stats("seller")
        assert stats["total_as_seller"] == 2
        assert abs(stats["total_revenue"] - 0.15) < 1e-6

    def test_role_filter(self, ledger):
        ledger.record("A", "B", ["1"], "semantic", 1, 0.01, 0.5, 0.5)
        ledger.record("B", "A", ["2"], "semantic", 1, 0.01, 0.5, 0.5)
        assert len(ledger.get_transactions("A", role="seller")) == 1
        assert len(ledger.get_transactions("A", role="buyer")) == 1
        assert len(ledger.get_transactions("A", role="any")) == 2


# ═══════════════════════════════════════════════════════════
# PricingEngine Tests
# ═══════════════════════════════════════════════════════════

class TestPricingEngine:
    def test_base_prices(self, pricing):
        # Episodic base price with neutral reputation
        price = pricing.price_memory("episodic", 1.0, 0.5, "general")
        # base=0.0001, rep_mult=0.5 (below 0.70), rarity=common(1.0), conf=1.0
        assert abs(price - 0.0001 * 0.5 * 1.0) < 1e-8

    def test_semantic_price(self, pricing):
        price = pricing.price_memory("semantic", 1.0, 0.85, "general")
        # base=0.01, rep_mult=1.0 (>=0.80), rarity=common(1.0), conf=1.0
        assert abs(price - 0.01) < 1e-8

    def test_procedural_price(self, pricing):
        price = pricing.price_memory("procedural", 0.95, 0.96, "cryptography")
        # base=1.00, rep_mult=2.0 (>=0.95), rarity=rare(3.0), conf=0.95
        assert abs(price - 1.00 * 2.0 * 3.0 * 0.95) < 1e-6

    def test_reputation_multiplier_tiers(self, pricing):
        # Test each tier
        p1 = pricing.price_memory("semantic", 1.0, 0.96, "general")  # 2.0x
        p2 = pricing.price_memory("semantic", 1.0, 0.91, "general")  # 1.5x
        p3 = pricing.price_memory("semantic", 1.0, 0.81, "general")  # 1.0x
        p4 = pricing.price_memory("semantic", 1.0, 0.71, "general")  # 0.5x
        p5 = pricing.price_memory("semantic", 1.0, 0.60, "general")  # 0.5x (default)

        assert p1 > p2 > p3 > p4
        assert abs(p4 - p5) < 1e-8  # both default 0.5

    def test_rarity_tiers(self, pricing):
        assert pricing.get_rarity("general") == "common"
        assert pricing.get_rarity("quantum_computing") == "rare"
        assert pricing.get_rarity("django") == "uncommon"

    def test_value_collection(self, pricing):
        memories = [
            {"memory_type": "semantic", "confidence": 1.0, "category": "general"},
            {"memory_type": "episodic", "confidence": 0.8, "category": "general"},
            {"memory_type": "procedural", "confidence": 1.0, "category": "cryptography"},
        ]
        result = pricing.value_collection(memories, reputation=0.85)
        assert result["total_value_usd"] > 0
        assert "semantic" in result["breakdown_by_type"]

    def test_confidence_scales_price(self, pricing):
        high = pricing.price_memory("semantic", 1.0, 0.85, "general")
        low = pricing.price_memory("semantic", 0.5, 0.85, "general")
        assert abs(high - low * 2) < 1e-8


# ═══════════════════════════════════════════════════════════
# ReputationEngine Tests
# ═══════════════════════════════════════════════════════════

class TestReputationEngine:
    def test_new_agent_neutral_reputation(self, reputation, registry):
        registry.register("fresh_agent")
        score = reputation.calculate("fresh_agent")
        assert score == 0.5  # neutral for new agents

    def test_reputation_after_transactions(self, reputation, ledger, registry):
        registry.register("seller")
        # Record several transactions
        for i in range(10):
            ledger.record("seller", f"buyer_{i}", [str(i)], "semantic", 1, 0.01, 0.5, 0.5)

        score = reputation.calculate("seller")
        assert score > 0.5  # should be higher than neutral
        assert score <= 1.0

    def test_reputation_update_persists(self, reputation, ledger, registry):
        registry.register("seller_x")
        for i in range(6):
            ledger.record("seller_x", f"buyer_{i}", [str(i)], "semantic", 1, 0.01, 0.5, 0.5)

        reputation.update("seller_x")
        info = registry.get("seller_x")
        assert info["reputation_score"] > 0.5

    def test_reputation_get_breakdown(self, reputation, ledger, registry):
        registry.register("seller_y")
        for i in range(3):
            ledger.record("seller_y", f"b{i}", [str(i)], "semantic", 1, 0.01, 0.5, 0.5)

        result = reputation.get("seller_y")
        assert "score" in result
        assert "breakdown" in result
        assert "base" in result["breakdown"]
        assert "quality" in result["breakdown"]
        assert "consistency" in result["breakdown"]


# ═══════════════════════════════════════════════════════════
# MemoryTransfer Tests
# ═══════════════════════════════════════════════════════════

class TestMemoryTransfer:
    def test_list_for_sale(self, registry, pricing):
        backend = MockBackend()
        backend.store(_make_entry("Django ORM caching pattern", "django", "semantic", 0.95))
        backend.store(_make_entry("Handle null in forms", "null_handling", "episodic", 0.8))
        backend.store(_make_entry("Low confidence memory", "general", "episodic", 0.3))  # below threshold

        registry.register("agent_A", name="Django Agent", categories=["django"])

        result = MemoryTransfer.list_for_sale(backend, "agent_A", registry, pricing)
        assert result["total_memories"] == 2  # low-confidence excluded
        assert len(result["for_sale"]) == 2
        assert all(m["confidence"] >= 0.7 for m in result["for_sale"])

    def test_list_for_sale_filter_by_category(self, registry, pricing):
        backend = MockBackend()
        backend.store(_make_entry("Python tip", "python", "semantic", 0.9))
        backend.store(_make_entry("JS tip", "javascript", "semantic", 0.9))

        registry.register("agent_A")
        result = MemoryTransfer.list_for_sale(backend, "agent_A", registry, pricing, category="python")
        assert result["total_memories"] == 1
        assert result["for_sale"][0]["category"] == "python"

    def test_list_for_sale_filter_by_type(self, registry, pricing):
        backend = MockBackend()
        backend.store(_make_entry("Episodic fact", "general", "episodic", 0.9))
        backend.store(_make_entry("Semantic pattern", "general", "semantic", 0.9))

        registry.register("agent_A")
        result = MemoryTransfer.list_for_sale(backend, "agent_A", registry, pricing, memory_type="semantic")
        assert result["total_memories"] == 1
        assert result["for_sale"][0]["type"] == "semantic"

    def test_purchase_end_to_end(self, registry, ledger, pricing):
        # Set up seller with memories
        seller_backend = MockBackend()
        seller_backend.store(_make_entry("Django caching strategy", "django", "semantic", 0.95))
        seller_backend.store(_make_entry("Redis connection pooling", "django", "procedural", 0.90))

        # Set up buyer (empty)
        buyer_backend = MockBackend()

        registry.register("seller_1", name="Django Expert")
        registry.register("buyer_1", name="New Agent")

        result = MemoryTransfer.purchase(
            seller_backend=seller_backend,
            buyer_backend=buyer_backend,
            seller_id="seller_1",
            buyer_id="buyer_1",
            registry=registry,
            ledger=ledger,
            pricing_engine=pricing,
            max_price_usd=100.0,
        )

        assert result["memories_purchased"] == 2
        assert result["total_cost_usd"] > 0
        assert result["transaction_id"].startswith("txn_")
        # Verify buyer now has memories
        buyer_results = buyer_backend.search(query="", top_k=100)
        assert len(buyer_results) == 2

    def test_purchase_budget_limit(self, registry, ledger, pricing):
        seller_backend = MockBackend()
        # Store expensive procedural memories
        for i in range(5):
            seller_backend.store(_make_entry(f"Procedural skill {i}", "cryptography", "procedural", 0.95))

        buyer_backend = MockBackend()
        registry.register("seller", name="Expert")
        registry.register("buyer")

        result = MemoryTransfer.purchase(
            seller_backend=seller_backend,
            buyer_backend=buyer_backend,
            seller_id="seller",
            buyer_id="buyer",
            registry=registry,
            ledger=ledger,
            pricing_engine=pricing,
            max_price_usd=2.0,  # Very limited budget
        )

        # Should purchase fewer than all 5 due to budget
        assert result["total_cost_usd"] <= 2.0

    def test_purchase_quality_gate(self, registry, ledger, pricing):
        seller_backend = MockBackend()
        seller_backend.store(_make_entry("High quality", "general", "semantic", 0.95))
        seller_backend.store(_make_entry("Low quality", "general", "semantic", 0.3))  # below 0.7

        buyer_backend = MockBackend()
        registry.register("seller")
        registry.register("buyer")

        result = MemoryTransfer.purchase(
            seller_backend=seller_backend,
            buyer_backend=buyer_backend,
            seller_id="seller",
            buyer_id="buyer",
            registry=registry,
            ledger=ledger,
            pricing_engine=pricing,
        )

        # Only high quality memory should transfer
        assert result["memories_purchased"] == 1

    def test_value_memories(self, registry, pricing):
        backend = MockBackend()
        backend.store(_make_entry("Fact 1", "general", "episodic", 0.9))
        backend.store(_make_entry("Pattern 1", "django", "semantic", 0.95))
        backend.store(_make_entry("Skill 1", "python", "procedural", 1.0))

        registry.register("agent_X", name="Full Agent")

        result = MemoryTransfer.value_memories(backend, "agent_X", registry, pricing)
        assert result["total_memories"] == 3
        assert result["total_value_usd"] > 0
        assert "episodic" in result["breakdown"]
        assert "semantic" in result["breakdown"]
        assert "procedural" in result["breakdown"]

    def test_value_memories_empty_backend(self, registry, pricing):
        backend = MockBackend()
        registry.register("empty_agent")

        result = MemoryTransfer.value_memories(backend, "empty_agent", registry, pricing)
        assert result["total_memories"] == 0
        assert result["total_value_usd"] == 0


# ═══════════════════════════════════════════════════════════
# Integration Test — Full Marketplace Flow
# ═══════════════════════════════════════════════════════════

class TestMarketplaceIntegration:
    def test_full_flow(self, tmp_commerce_db):
        """End-to-end: register → store → list → discover → purchase → verify."""
        registry = AgentRegistry(tmp_commerce_db)
        ledger = TransactionLedger(tmp_commerce_db)
        reputation = ReputationEngine(tmp_commerce_db)
        pricing = PricingEngine()

        # 1. Register agents
        registry.register("django_expert", name="Django Expert", description="6 months Django experience", categories=["django", "python"])
        registry.register("newbie", name="New Agent", description="Day 1 agent", categories=[])

        # 2. Seller stores memories
        seller_backend = MockBackend()
        memories_text = [
            ("Django ORM N+1 query fix", "django", "semantic", 0.95),
            ("Use select_related for FK joins", "django", "procedural", 0.90),
            ("Deployed to Railway successfully", "deployment", "episodic", 0.80),
            ("Low quality throwaway", "general", "episodic", 0.2),  # should be filtered
        ]
        for text, cat, mtype, conf in memories_text:
            seller_backend.store(_make_entry(text, cat, mtype, conf))

        # 3. List for sale
        registry.set_for_sale("django_expert", True)
        with registry._get_conn() as conn:
            conn.execute("UPDATE agent_registry SET reputation_score = 0.9 WHERE agent_id = 'django_expert'")

        listing = MemoryTransfer.list_for_sale(seller_backend, "django_expert", registry, pricing)
        assert listing["total_memories"] == 3  # low-quality excluded
        assert listing["total_value_usd"] > 0

        # 4. Discover sellers
        sellers = registry.search_sellers(category="django", min_reputation=0.7)
        assert len(sellers) == 1
        assert sellers[0]["agent_id"] == "django_expert"

        # 5. Purchase
        buyer_backend = MockBackend()
        receipt = MemoryTransfer.purchase(
            seller_backend=seller_backend,
            buyer_backend=buyer_backend,
            seller_id="django_expert",
            buyer_id="newbie",
            registry=registry,
            ledger=ledger,
            pricing_engine=pricing,
            max_price_usd=100.0,
        )
        assert receipt["memories_purchased"] == 3
        assert receipt["total_cost_usd"] > 0
        assert receipt["transaction_id"].startswith("txn_")

        # 6. Verify buyer has memories
        buyer_memories = buyer_backend.search(query="", top_k=100)
        assert len(buyer_memories) == 3

        # 7. Verify transaction ledger
        txns = ledger.get_transactions("django_expert", role="seller")
        assert len(txns) == 1
        assert txns[0]["memories_count"] == 3

        # 8. Update and check reputation
        reputation.update("django_expert")
        rep = reputation.get("django_expert")
        assert rep["score"] > 0  # has some reputation from 1 transaction
        assert rep["total_transactions"] == 1

    def test_zero_api_calls(self):
        """Verify that the entire commerce module uses zero LLM API calls.
        This is a structural test — we check that no HTTP/API clients are imported."""
        import cognicore.commerce.marketplace as mp
        import cognicore.commerce.transfer as tr

        # These modules should NOT import any HTTP/API client libraries
        marketplace_source = open(mp.__file__).read()
        transfer_source = open(tr.__file__).read()

        forbidden_imports = ["openai", "anthropic", "requests.post", "httpx", "urllib.request"]
        for forbidden in forbidden_imports:
            assert forbidden not in marketplace_source, f"marketplace.py imports {forbidden}"
            assert forbidden not in transfer_source, f"transfer.py imports {forbidden}"

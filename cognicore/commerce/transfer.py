import hashlib
import json
import logging
from typing import Optional, List, Dict, Any
from cognicore.memory.base import MemoryEntry, MemoryScope

logger = logging.getLogger('cognicore.commerce.transfer')

class MemoryTransfer:
    """Utility class for Memory Commerce and direct memory sharing.
    
    Two modes of operation:
    
    1. DIRECT SHARING (no marketplace needed):
       MemoryTransfer.share(agent_a_backend, agent_b_backend)
       MemoryTransfer.clone(source_backend, target_backend)
       
    2. MARKETPLACE (pricing, reputation, discovery):
       MemoryTransfer.list_for_sale(...)
       MemoryTransfer.purchase(...)
       MemoryTransfer.value_memories(...)
    """

    @staticmethod
    def share(
        source_backend: Any,
        target_backend: Any,
        source_agent_id: str = "source",
        category: str = "",
        memory_type: str = "all",
        min_confidence: float = 0.0,
        top_k: int = 10000,
        merge_strategy: str = "copy",
    ) -> Dict[str, Any]:
        """Share memories directly between two backends in Python code.
        
        This is the simplest way to transfer knowledge between agents.
        No marketplace, no pricing, no registration needed.
        
        Args:
            source_backend: The backend to copy memories FROM.
            target_backend: The backend to copy memories INTO.
            source_agent_id: ID to tag as source_agent on copied memories.
            category: Only share memories in this category (empty = all).
            memory_type: Filter by type: 'episodic', 'semantic', 'procedural', or 'all'.
            min_confidence: Minimum confidence threshold (0.0 = share everything).
            top_k: Maximum number of memories to transfer.
            merge_strategy: 'copy' (duplicate into target) or 'move' (copy + delete from source).
            
        Returns:
            Dict with transferred count, categories, and summary.
            
        Example:
            >>> from cognicore.commerce.transfer import MemoryTransfer
            >>> result = MemoryTransfer.share(agent_a.backend, agent_b.backend)
            >>> print(f"Transferred {result['transferred']} memories")
        """
        results = source_backend.search(query='', top_k=top_k, scope=None)
        entries = [r.entry for r in results]
        
        # Apply filters
        filtered = []
        for entry in entries:
            if entry.confidence < min_confidence:
                continue
            if category and entry.category != category:
                continue
            if memory_type != 'all' and entry.memory_type != memory_type:
                continue
            filtered.append(entry)
        
        # Transfer
        transferred = []
        categories_transferred = set()
        types_transferred = set()
        
        for entry in filtered:
            new_entry = MemoryEntry(
                text=entry.text,
                category=entry.category,
                memory_type=entry.memory_type,
                confidence=entry.confidence,
                action=entry.action,
                metadata=entry.metadata.copy() if entry.metadata else {},
                source_agent=source_agent_id,
                creation_reason="shared",
                scope=entry.scope,
                scope_id=entry.scope_id,
            )
            new_id = target_backend.store(new_entry)
            transferred.append(new_id)
            categories_transferred.add(entry.category)
            types_transferred.add(entry.memory_type)
            
            if merge_strategy == "move" and hasattr(source_backend, 'delete'):
                source_backend.delete(entry.entry_id)
        
        logger.info(f"Shared {len(transferred)} memories from {source_agent_id} → target")
        
        return {
            "transferred": len(transferred),
            "categories": sorted(categories_transferred),
            "memory_types": sorted(types_transferred),
            "source_agent": source_agent_id,
            "strategy": merge_strategy,
            "new_entry_ids": transferred,
        }

    @staticmethod
    def clone(
        source_backend: Any,
        target_backend: Any,
        source_agent_id: str = "source",
    ) -> Dict[str, Any]:
        """Clone ALL memories from one backend to another.
        
        Convenience wrapper — shares everything with no filters.
        Use this when deploying a new agent that should start with
        all knowledge from an experienced agent.
        
        Args:
            source_backend: The backend to clone FROM.
            target_backend: The backend to clone INTO.
            source_agent_id: ID to tag as source on cloned memories.
            
        Returns:
            Dict with transfer results.
            
        Example:
            >>> result = MemoryTransfer.clone(veteran.backend, rookie.backend, "veteran_agent")
            >>> # rookie now has all of veteran's memories
        """
        return MemoryTransfer.share(
            source_backend=source_backend,
            target_backend=target_backend,
            source_agent_id=source_agent_id,
            min_confidence=0.0,
            top_k=100000,
        )

    @staticmethod
    def list_for_sale(backend: Any, agent_id: str, registry: Any, pricing_engine: Any, 
                      category: str = '', memory_type: str = 'all', 
                      min_confidence: float = 0.7, top_k: int = 20) -> Dict[str, Any]:
        """
        List memories available for sale from a given agent's backend.
        """
        # Search backend for all memories
        results = backend.search(query='', top_k=10000, scope=None)
        entries = [r.entry for r in results]
        
        filtered = []
        for entry in entries:
            if entry.confidence < min_confidence:
                continue
            if category and entry.category != category:
                continue
            if memory_type != 'all' and entry.memory_type != memory_type:
                continue
            filtered.append(entry)
            
        reputation_score = 0.5
        if registry and hasattr(registry, 'get'):
            agent_info = registry.get(agent_id)
            if isinstance(agent_info, dict):
                reputation_score = agent_info.get('reputation_score', 0.5)
            
        for_sale = []
        for entry in filtered:
            price = pricing_engine.price_memory(entry.memory_type, entry.confidence, reputation_score, entry.category)
            for_sale.append({
                'id': entry.entry_id,
                'type': entry.memory_type,
                'preview': entry.text[:60] if entry.text else "",
                'category': entry.category,
                'confidence': entry.confidence,
                'price_usd': price
            })
            
        total_memories = len(for_sale)
        total_value_usd = sum(x['price_usd'] for x in for_sale)
        
        # Sort by confidence DESC
        for_sale.sort(key=lambda x: x['confidence'], reverse=True)
        for_sale_top_k = for_sale[:top_k]
        
        return {
            "agent_id": agent_id,
            "for_sale": for_sale_top_k,
            "total_memories": total_memories,
            "total_value_usd": total_value_usd,
            "reputation_score": reputation_score
        }

    @staticmethod
    def purchase(seller_backend: Any, buyer_backend: Any, seller_id: str, buyer_id: str, 
                 registry: Any, ledger: Any, pricing_engine: Any, 
                 memory_ids: Optional[List[str]] = None, memory_type: str = 'all', 
                 category_filter: str = '', max_price_usd: float = 10.0) -> Dict[str, Any]:
        """
        Purchase memories from a seller and transfer them to a buyer.
        """
        # Step 1: Get seller's memories
        entries = []
        if memory_ids:
            for mid in memory_ids:
                e = seller_backend.get_by_id(mid)
                if e:
                    entries.append(e)
        else:
            results = seller_backend.search(query='', top_k=10000, scope=None)
            entries = [r.entry for r in results]
            
        # Step 2: Filter memories
        filtered = []
        for entry in entries:
            if entry.confidence < 0.7:
                continue
            if memory_type != 'all' and entry.memory_type != memory_type:
                continue
            if category_filter and entry.category != category_filter:
                continue
            filtered.append(entry)
            
        seller_rep = 0.5
        if registry and hasattr(registry, 'get'):
            seller_info = registry.get(seller_id)
            if isinstance(seller_info, dict):
                seller_rep = seller_info.get('reputation_score', 0.5)
                
        # Step 3: Calculate total price
        memory_items = []
        for entry in filtered:
            price = pricing_engine.price_memory(entry.memory_type, entry.confidence, seller_rep, entry.category)
            memory_items.append({'price': price, 'entry': entry})
            
        # Step 4: Fit to budget (drop lowest-confidence first)
        memory_items.sort(key=lambda x: x['entry'].confidence)
        
        total_price = sum(item['price'] for item in memory_items)
        while total_price > max_price_usd and memory_items:
            dropped = memory_items.pop(0)
            total_price -= dropped['price']
            
        purchased_entries = [item['entry'] for item in memory_items]
        
        # Step 5: Create new MemoryEntry and store
        purchased_results = []
        for entry in purchased_entries:
            new_entry = MemoryEntry(
                text=entry.text,
                category=entry.category,
                memory_type=entry.memory_type,
                confidence=entry.confidence,
                action=entry.action,
                metadata=entry.metadata.copy() if entry.metadata else {},
                source_agent=seller_id,
                creation_reason='purchased'
            )
            buyer_backend.store(new_entry)
            purchased_results.append({
                'id': entry.entry_id,
                'type': entry.memory_type,
                'text_preview': entry.text[:60] if entry.text else "",
                'category': entry.category
            })
            
        # Step 6: Record transaction via ledger
        memory_ids_list = [entry.entry_id for entry in purchased_entries]
        memory_type_str = memory_type if memory_type != 'all' else 'mixed'

        buyer_rep = 0.5
        if registry and hasattr(registry, 'get'):
            buyer_info = registry.get(buyer_id)
            if isinstance(buyer_info, dict):
                buyer_rep = buyer_info.get('reputation_score', 0.5)

        txn_id = ""
        if ledger is not None and hasattr(ledger, 'record'):
            txn_id = ledger.record(
                seller_id=seller_id,
                buyer_id=buyer_id,
                memory_ids=memory_ids_list,
                memory_type=memory_type_str,
                count=len(purchased_entries),
                price_usd=total_price,
                seller_rep=seller_rep,
                buyer_rep=buyer_rep,
            )

        # Step 7: Return result
        months = len(purchased_entries) / 30.0

        return {
            "transaction_id": txn_id,
            "memories_purchased": len(purchased_entries),
            "total_cost_usd": total_price,
            "memories": purchased_results,
            "seller_reputation": seller_rep,
            "estimated_value": f"equivalent to {months:.1f} months experience"
        }

    @staticmethod
    def value_memories(backend: Any, agent_id: str, registry: Any, pricing_engine: Any, category: str = '') -> Dict[str, Any]:
        """
        Value all memories for a given agent and return a detailed breakdown.
        """
        results = backend.search(query='', top_k=10000, scope=None)
        entries = [r.entry for r in results]
        
        if category:
            entries = [e for e in entries if e.category == category]
            
        reputation_score = 0.5
        if registry and hasattr(registry, 'get'):
            agent_info = registry.get(agent_id)
            if isinstance(agent_info, dict):
                reputation_score = agent_info.get('reputation_score', 0.5)
                
        breakdown = {
            "episodic": {"count": 0, "value_usd": 0.0},
            "semantic": {"count": 0, "value_usd": 0.0},
            "procedural": {"count": 0, "value_usd": 0.0},
            "other": {"count": 0, "value_usd": 0.0}
        }
        
        total_value = 0.0
        
        category_totals = {}
        for entry in entries:
            m_type = entry.memory_type
            if m_type not in breakdown:
                if m_type not in ["episodic", "semantic", "procedural"]:
                    m_type = "other"
            
            price = pricing_engine.price_memory(entry.memory_type, entry.confidence, reputation_score, entry.category)
            
            breakdown[m_type]["count"] += 1
            breakdown[m_type]["value_usd"] += price
            total_value += price
            
            cat = entry.category or "general"
            category_totals[cat] = category_totals.get(cat, 0.0) + price
            
        most_valuable_category = None
        max_val = -1
        for cat, val in category_totals.items():
            if val > max_val:
                max_val = val
                most_valuable_category = cat
                
        # Recommended prices based on the sample json
        # In a real app we'd compute this or fetch from pricing engine, but we'll mock it per the spec
        recommended_price = {
            "episodic_per_memory": 0.0001,
            "semantic_per_pattern": 0.01,
            "procedural_per_skill": 1.00
        }
        
        # Formatting values to 2 decimal places to match sample (or keeping as float)
        # It's better to keep as floats and round or just let standard JSON serialization handle it
        for k in breakdown:
            breakdown[k]["value_usd"] = round(breakdown[k]["value_usd"], 2)
            
        return {
            "total_memories": len(entries),
            "breakdown": breakdown,
            "total_value_usd": round(total_value, 2),
            "most_valuable_category": most_valuable_category,
            "reputation_score": reputation_score,
            "recommended_price": recommended_price
        }

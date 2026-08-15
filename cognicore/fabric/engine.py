import json
import logging
import hashlib
from datetime import datetime
from typing import Dict, Any, List, Optional, Type

from cognicore.memory.base import MemoryBackend, MemoryEntry, MemoryScope

logger = logging.getLogger('cognicore.fabric')

class CognitiveFabric:
    """
    The Semantic OS Engine (The Shared Brain).
    Handles Layers 2-5: discovering patterns, generating rules, and translating them for adapters.
    """
    
    def __init__(self, backend: MemoryBackend):
        self.backend = backend
        self._adapters: Dict[str, Type['CognitiveAdapter']] = {}
        
    def register_adapter(self, name: str, adapter_cls: Type['CognitiveAdapter']):
        """Registers a new tool adapter class with the Fabric."""
        self._adapters[name] = adapter_cls
        
    def connect(self, name: str) -> 'CognitiveAdapter':
        """Instantiates and returns the requested adapter connected to this Fabric."""
        if name not in self._adapters:
            raise ValueError(f"Adapter '{name}' is not registered with the Cognitive Fabric.")
        return self._adapters[name](fabric=self)

    # --- Layer 1: Data Ingestion ---
    
    def record_observation(self, tool_name: str, action: str, context: Dict[str, Any]) -> str:
        """Stores a raw Layer 1 observation into the shared memory."""
        obs_id = f"obs_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{hashlib.md5((tool_name+action).encode()).hexdigest()[:6]}"
        
        entry = MemoryEntry(
            entry_id=obs_id,
            memory_type="fabric_observation",
            text=f"{tool_name} performed {action}",
            category=tool_name,
            confidence=1.0,
            scope=MemoryScope.GLOBAL,
            action=action,
            metadata=context
        )
        self.backend.store(entry)
        return obs_id

    # --- Layer 2 & 4: Pattern Discovery and Feedback ---
    
    def derive_rules(self) -> List[Dict[str, Any]]:
        """
        Layer 2: Analyze observations to discover patterns and generate universal rules.
        In a full implementation, this uses an LLM or ML classifier to cluster context.
        For now, we use a simple heuristic to demonstrate the flow.
        """
        # Fetch all observations across all tools
        observations = self.backend.get_by_category("figma", top_k=100) # Mock: getting figma observations
        if not observations:
            return []
            
        # Discover patterns dynamically based on observations
        is_minimalist = False
        active_patterns = set()
        
        for obs in observations:
            meta = obs.metadata or {}
            
            # Generalize pattern extraction based on LLM/heuristics in metadata
            if "pattern" in meta:
                active_patterns.add(meta["pattern"])
                
            # Legacy fallback for backward compatibility
            if meta.get("whitespace") == "high" or \
               meta.get("color_palette") == "pastel":
                active_patterns.add("Minimalist")
                
        rules = []
        for pattern in active_patterns:
            rules.append({
                "concept": pattern,
                "confidence": 0.9,
                "description": f"The project favors {pattern.lower()} structures."
            })
            
        return rules

    # --- Layer 3: Translation & Automation ---
    
    def translate_for_tool(self, tool_name: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Layer 3: Translates the universal rules (Layer 2) into tool-specific recommendations.
        """
        rules = self.derive_rules()
        
        # If no rules discovered, return empty
        if not rules:
            return {}
            
        active_concepts = [r["concept"] for r in rules]
        
        # Dynamic translation
        translations = {}
        for concept in active_concepts:
            if tool_name == "elevenlabs":
                # If we have a voice preference in the context, use it; otherwise fallback
                translations.update({
                    "reason": f"Translated '{concept}' project vibe into voice settings.",
                    "voice": context.get("preferred_voice", "Rachel"),
                    "speed": 0.85,
                    "stability": 0.80,
                    "tone": "warm and slow"
                })
            elif tool_name in ["cursor", "claude"]:
                instructions = context.get("coding_instructions", [])
                if not instructions:
                    instructions = [
                        f"Use UI patterns matching the '{concept}' aesthetic.",
                        "Follow the Figma design tokens implicitly."
                    ]
                translations.update({
                    "reason": f"Translated '{concept}' project vibe into coding instructions.",
                    "instructions": instructions
                })
                
        return translations

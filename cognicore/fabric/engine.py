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
            
        # Discover "Calm/Minimalist" pattern if we see pastel colors and high whitespace
        is_minimalist = False
        for obs in observations:
            meta = obs.metadata or {}
            if meta.get("whitespace") == "high" or \
               meta.get("color_palette") == "pastel":
                is_minimalist = True
                break
                
        rules = []
        if is_minimalist:
            rules.append({
                "concept": "Minimalist",
                "confidence": 0.9,
                "description": "The project favors calm, clean, and simple structures."
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
        
        # Translating the "Minimalist" concept for different domains
        if "Minimalist" in active_concepts:
            if tool_name == "elevenlabs":
                return {
                    "reason": "Translated 'Minimalist' project vibe into calm voice settings.",
                    "voice": "Rachel",
                    "speed": 0.85,
                    "stability": 0.80,
                    "tone": "warm and slow"
                }
            elif tool_name in ["cursor", "claude"]:
                return {
                    "reason": "Translated 'Minimalist' project vibe into coding instructions.",
                    "instructions": [
                        "Use clean UI patterns (e.g., lots of padding, #F9F9F9 background).",
                        "Avoid flashy animations.",
                        "Keep component density low.",
                        "Prioritize readability over complex layouts.",
                        "Follow the Figma design tokens implicitly."
                    ]
                }
                
        return {}

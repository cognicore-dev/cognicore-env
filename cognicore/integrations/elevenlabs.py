"""
CogniCore ElevenLabs Integration — Persistent memory layer for ElevenLabs API.

Stores voice preferences, usage patterns, and advanced settings as structured
MemoryEntry objects. Provides recall methods that return parameters in the exact
format the ElevenLabs API expects, so developers can do:

    params = integration.recall()
    elevenlabs_client.generate(text="Hello", **params)

Three tiers of memory:
  - Tier 1: Voice Preferences (voice_id, stability, similarity_boost, speed, etc.)
  - Tier 2: Usage Patterns (best voices, rejections, pronunciations)
  - Tier 3: Advanced Features (remix, streaming, cloning, pronunciation dicts)
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from cognicore.memory.base import MemoryEntry, MemoryScope

logger = logging.getLogger("cognicore.integrations.elevenlabs")

# Category constants
CATEGORY_VOICE = "elevenlabs_voice"
CATEGORY_USAGE = "elevenlabs_usage"
CATEGORY_ADVANCED = "elevenlabs_advanced"


class ElevenLabsIntegration:
    """Persistent memory layer for ElevenLabs voice preferences and settings.

    Wraps any CogniCore memory backend to store and recall ElevenLabs-specific
    configuration across sessions. Zero API calls — pure local storage.

    Example::

        from cognicore.memory import SQLiteMemoryBackend
        from cognicore.integrations.elevenlabs import ElevenLabsIntegration

        backend = SQLiteMemoryBackend("my_agent.db")
        el = ElevenLabsIntegration(backend)

        # Store preferences once
        el.sync(
            voice_id="pNInz6obpgDQGcFmaJgB",
            voice_name="Adam",
            stability=0.75,
            similarity_boost=0.85,
            content_type="meditation",
            tone="warm and calm"
        )

        # Recall as ready-to-use API params
        params = el.recall()
        # {"voice_id": "pNInz...", "voice_settings": {...}, "model_id": "..."}
    """

    def __init__(self, backend: Any) -> None:
        """Initialize with a CogniCore memory backend.

        Args:
            backend: Any CogniCore memory backend (SQLiteMemoryBackend,
                     TFIDFMemoryBackend, etc.) that supports store/search.
        """
        self.backend = backend

    # ------------------------------------------------------------------
    # Tier 1: Voice Preferences
    # ------------------------------------------------------------------

    def sync(
        self,
        voice_id: str,
        voice_name: str = "",
        stability: float = 0.75,
        similarity_boost: float = 0.85,
        style_exaggeration: float = 0.0,
        speed: float = 1.0,
        use_speaker_boost: bool = True,
        content_type: str = "",
        audience: str = "",
        tone: str = "",
        language: str = "en",
        model_id: str = "eleven_multilingual_v2",
    ) -> Dict[str, Any]:
        """Store ElevenLabs voice preferences and settings.

        Overwrites any existing voice preference entry to keep a single
        canonical source of truth.

        Args:
            voice_id: ElevenLabs voice ID (e.g. "pNInz6obpgDQGcFmaJgB").
            voice_name: Human-readable name (e.g. "Adam").
            stability: 0-1, higher = more consistent voice.
            similarity_boost: 0-1, higher = closer to original voice.
            style_exaggeration: 0-1, 0 = natural delivery.
            speed: Speech speed multiplier. <1.0 = slower, >1.0 = faster.
            use_speaker_boost: Enable ElevenLabs speaker boost.
            content_type: Context (e.g. "meditation", "podcast", "narration").
            audience: Target audience (e.g. "25-40 professionals").
            tone: Desired tone (e.g. "warm and calm", "energetic").
            language: Language code (default "en").
            model_id: ElevenLabs model ID.

        Returns:
            Dict with status, stored entry ID, and summary.
        """
        # Remove any existing voice preference entries
        self._clear_category(CATEGORY_VOICE)

        # Build descriptive text for search/recall
        parts = [f"ElevenLabs voice preference: {voice_name or voice_id}"]
        if content_type:
            parts.append(f"for {content_type} content")
        if tone:
            parts.append(f"with {tone} tone")
        if audience:
            parts.append(f"targeting {audience}")
        text = " ".join(parts)

        entry = MemoryEntry(
            text=text,
            category=CATEGORY_VOICE,
            memory_type="preference",
            confidence=1.0,
            metadata={
                "voice_id": voice_id,
                "voice_name": voice_name,
                "stability": stability,
                "similarity_boost": similarity_boost,
                "style_exaggeration": style_exaggeration,
                "speed": speed,
                "use_speaker_boost": use_speaker_boost,
                "content_type": content_type,
                "audience": audience,
                "tone": tone,
                "language": language,
                "model_id": model_id,
            },
        )
        entry_id = self.backend.store(entry)
        logger.info(f"Stored ElevenLabs voice preference: {voice_name} ({voice_id})")

        return {
            "status": "success",
            "entry_id": str(entry_id),
            "voice_name": voice_name,
            "voice_id": voice_id,
            "model_id": model_id,
            "message": f"Voice preference for '{voice_name or voice_id}' saved.",
        }

    def recall(self) -> Dict[str, Any]:
        """Retrieve stored ElevenLabs voice preferences as ready-to-use API parameters.

        Returns a dict that can be unpacked directly into the ElevenLabs
        Python SDK ``generate()`` or ``text_to_speech.convert()`` call.

        Returns:
            Dict with voice_id, voice_settings, model_id, and content context.
            Returns empty dict with defaults if no preferences stored.
        """
        entries = self.backend.get_by_category(CATEGORY_VOICE, top_k=1)
        if not entries:
            return {
                "voice_id": "",
                "voice_settings": {
                    "stability": 0.75,
                    "similarity_boost": 0.85,
                    "style": 0.0,
                    "speed": 1.0,
                    "use_speaker_boost": True,
                },
                "model_id": "eleven_multilingual_v2",
                "content_context": {},
                "has_preferences": False,
            }

        entry = entries[0]
        meta = entry.metadata if hasattr(entry, "metadata") else {}

        return {
            "voice_id": meta.get("voice_id", ""),
            "voice_settings": {
                "stability": meta.get("stability", 0.75),
                "similarity_boost": meta.get("similarity_boost", 0.85),
                "style": meta.get("style_exaggeration", 0.0),
                "speed": meta.get("speed", 1.0),
                "use_speaker_boost": meta.get("use_speaker_boost", True),
            },
            "model_id": meta.get("model_id", "eleven_multilingual_v2"),
            "content_context": {
                "voice_name": meta.get("voice_name", ""),
                "content_type": meta.get("content_type", ""),
                "audience": meta.get("audience", ""),
                "tone": meta.get("tone", ""),
                "language": meta.get("language", "en"),
            },
            "has_preferences": True,
        }

    # ------------------------------------------------------------------
    # Tier 2: Usage Patterns
    # ------------------------------------------------------------------

    def log_usage(
        self,
        voice_used: str,
        content_type: str = "",
        audio_length_sec: float = 0.0,
        success: bool = True,
        rejection_reason: str = "",
        custom_pronunciations: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Log a voice generation usage event for pattern learning.

        Over time, these entries build a behavioral profile that helps
        the system recommend better voices and settings.

        Args:
            voice_used: Voice name or ID that was used.
            content_type: Type of content generated.
            audio_length_sec: Duration of generated audio in seconds.
            success: Whether the generation was satisfactory.
            rejection_reason: If not successful, why it was rejected.
            custom_pronunciations: Dict of {"word": "pronunciation"} overrides.

        Returns:
            Dict with status and entry ID.
        """
        if success:
            text = f"Successfully used voice '{voice_used}' for {content_type or 'content'} ({audio_length_sec:.1f}s)"
        else:
            text = f"Rejected voice '{voice_used}': {rejection_reason}"

        entry = MemoryEntry(
            text=text,
            category=CATEGORY_USAGE,
            memory_type="episodic",
            confidence=1.0 if success else 0.3,
            correct=success,
            metadata={
                "voice_used": voice_used,
                "content_type": content_type,
                "audio_length_sec": audio_length_sec,
                "success": success,
                "rejection_reason": rejection_reason,
                "custom_pronunciations": custom_pronunciations or {},
                "timestamp": time.time(),
            },
        )
        entry_id = self.backend.store(entry)
        logger.info(f"Logged ElevenLabs usage: {voice_used} ({'ok' if success else 'rejected'})")

        return {"status": "success", "entry_id": str(entry_id)}

    def recall_usage(self) -> Dict[str, Any]:
        """Recall usage patterns from logged events.

        Analyzes all usage entries to extract:
        - Best performing voices
        - Rejected voices and reasons
        - Content categories used
        - Accumulated pronunciation overrides
        - Average audio length

        Returns:
            Dict with usage pattern summary.
        """
        entries = self.backend.get_by_category(CATEGORY_USAGE, top_k=100)
        if not entries:
            return {
                "total_generations": 0,
                "best_performing_voices": [],
                "rejected_voices": [],
                "content_categories": [],
                "custom_pronunciations": {},
                "avg_audio_length_sec": 0.0,
            }

        successes: Dict[str, int] = {}
        rejections: List[Dict[str, str]] = []
        categories: set = set()
        pronunciations: Dict[str, str] = {}
        total_length = 0.0
        total_count = 0

        for entry in entries:
            meta = entry.metadata if hasattr(entry, "metadata") else {}
            voice = meta.get("voice_used", "")
            is_success = meta.get("success", True)

            if is_success:
                successes[voice] = successes.get(voice, 0) + 1
                total_length += meta.get("audio_length_sec", 0.0)
                total_count += 1
            else:
                rejections.append({
                    "voice": voice,
                    "reason": meta.get("rejection_reason", ""),
                })

            ct = meta.get("content_type", "")
            if ct:
                categories.add(ct)

            # Merge pronunciations (latest wins)
            prons = meta.get("custom_pronunciations", {})
            if prons:
                pronunciations.update(prons)

        # Sort voices by usage count descending
        best_voices = sorted(successes.items(), key=lambda x: x[1], reverse=True)

        return {
            "total_generations": len(entries),
            "best_performing_voices": [
                {"voice": v, "uses": c} for v, c in best_voices
            ],
            "rejected_voices": rejections,
            "content_categories": sorted(categories),
            "custom_pronunciations": pronunciations,
            "avg_audio_length_sec": round(total_length / max(total_count, 1), 1),
        }

    # ------------------------------------------------------------------
    # Tier 3: Advanced Features
    # ------------------------------------------------------------------

    def set_advanced(
        self,
        remix_preferences: Optional[Dict[str, str]] = None,
        pronunciation_dict_id: str = "",
        use_streaming: bool = True,
        optimize_latency: bool = False,
        has_cloned_voice: bool = False,
        clone_type: Optional[str] = None,
        agent_text_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Store advanced ElevenLabs feature settings.

        Args:
            remix_preferences: Voice remixing prefs (gender, accent, pacing, style).
            pronunciation_dict_id: ElevenLabs pronunciation dictionary ID.
            use_streaming: Whether to use streaming TTS.
            optimize_latency: Optimize for lower latency vs quality.
            has_cloned_voice: Whether user has a cloned voice.
            clone_type: "instant" or "professional" if cloned.
            agent_text_overrides: ElevenAgents text behavior overrides.

        Returns:
            Dict with status and entry ID.
        """
        self._clear_category(CATEGORY_ADVANCED)

        text = "ElevenLabs advanced configuration"
        parts = []
        if remix_preferences:
            parts.append(f"remix: {remix_preferences}")
        if pronunciation_dict_id:
            parts.append(f"pronunciation dict: {pronunciation_dict_id}")
        if has_cloned_voice:
            parts.append(f"cloned voice ({clone_type})")
        if parts:
            text += " — " + ", ".join(parts)

        entry = MemoryEntry(
            text=text,
            category=CATEGORY_ADVANCED,
            memory_type="procedural",
            confidence=1.0,
            metadata={
                "remix_preferences": remix_preferences or {},
                "pronunciation_dict_id": pronunciation_dict_id,
                "use_streaming": use_streaming,
                "optimize_latency": optimize_latency,
                "has_cloned_voice": has_cloned_voice,
                "clone_type": clone_type,
                "agent_text_overrides": agent_text_overrides or {},
            },
        )
        entry_id = self.backend.store(entry)
        logger.info("Stored ElevenLabs advanced configuration")

        return {"status": "success", "entry_id": str(entry_id)}

    def recall_advanced(self) -> Dict[str, Any]:
        """Recall advanced ElevenLabs feature settings.

        Returns:
            Dict with advanced settings, or defaults if none stored.
        """
        entries = self.backend.get_by_category(CATEGORY_ADVANCED, top_k=1)
        if not entries:
            return {
                "remix_preferences": {},
                "pronunciation_dict_id": "",
                "use_streaming": True,
                "optimize_latency": False,
                "has_cloned_voice": False,
                "clone_type": None,
                "agent_text_overrides": {},
                "has_advanced_config": False,
            }

        meta = entries[0].metadata if hasattr(entries[0], "metadata") else {}
        return {
            "remix_preferences": meta.get("remix_preferences", {}),
            "pronunciation_dict_id": meta.get("pronunciation_dict_id", ""),
            "use_streaming": meta.get("use_streaming", True),
            "optimize_latency": meta.get("optimize_latency", False),
            "has_cloned_voice": meta.get("has_cloned_voice", False),
            "clone_type": meta.get("clone_type"),
            "agent_text_overrides": meta.get("agent_text_overrides", {}),
            "has_advanced_config": True,
        }

    # ------------------------------------------------------------------
    # Combined Recall
    # ------------------------------------------------------------------

    def recall_all(self) -> Dict[str, Any]:
        """Recall all stored ElevenLabs preferences across all three tiers.

        Returns:
            Combined dict with voice_preferences, usage_patterns, and advanced.
        """
        return {
            "voice_preferences": self.recall(),
            "usage_patterns": self.recall_usage(),
            "advanced": self.recall_advanced(),
        }

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _clear_category(self, category: str) -> None:
        """Remove all existing entries for a category (for single-source-of-truth updates)."""
        try:
            existing = self.backend.get_by_category(category, top_k=100)
            for entry in existing:
                entry_id = entry.entry_id if hasattr(entry, "entry_id") else None
                if entry_id and hasattr(self.backend, "delete"):
                    self.backend.delete(entry_id)
        except Exception as e:
            logger.warning(f"Could not clear category '{category}': {e}")

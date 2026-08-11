"""
CogniCore Figma Integration — Intelligence layer for the Figma REST API.

Matches the ElevenLabs adapter pattern exactly:
  sync()       — fetch real data from Figma API, store in CogniCore memory
  recall()     — retrieve stored design tokens as a clean dict
  learn_from_*()  — teach CogniCore about design decisions and outcomes
  recommend()  — ask CogniCore for cross-tool recommendations derived from Figma

Figma REST API: https://developers.figma.com/docs/rest-api/
Auth: Personal Access Token (X-Figma-Token header)
      Get yours at: https://www.figma.com/settings -> Personal access tokens

Usage:

    from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
    from cognicore.fabric.registry import get_fabric
    from cognicore.fabric.plugins.figma import FigmaAdapter

    fabric = get_fabric(SQLiteMemoryBackend("agent.db"))
    figma = FigmaAdapter(fabric)

    # Pull design tokens from a real Figma file
    result = figma.sync(
        file_key="abc123xyz",     # from figma.com/file/<KEY>/...
        access_token="figd_...",  # Personal Access Token
    )

    # Retrieve stored tokens in any future session (no token needed)
    tokens = figma.recall()
    # -> {"file_name": "My App", "colors": {...}, "typography": {...}, ...}

    # Ask CogniCore what ElevenLabs voice fits this design
    rec = figma.recommend(target_tool="elevenlabs")
    # -> {"voice": "Rachel", "speed": 0.85, "reason": "Minimalist design..."}

    # Log a design decision outcome for learning
    figma.learn_from_design(
        component="hero_section",
        change="increased padding from 16px to 32px",
        outcome="bounce_rate_down_12pct",
        success=True,
    )
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from cognicore.memory.base import MemoryEntry, MemoryScope
from cognicore.fabric.base import CognitiveAdapter

logger = logging.getLogger("cognicore.integrations.figma")

# ── Category constants (match ElevenLabs pattern) ────────────────────────────
CATEGORY_FILE       = "figma_file"
CATEGORY_COLORS     = "figma_colors"
CATEGORY_TYPOGRAPHY = "figma_typography"
CATEGORY_COMPONENTS = "figma_components"
CATEGORY_VARIABLES  = "figma_variables"
CATEGORY_STYLES     = "figma_styles"
CATEGORY_DECISION   = "figma_decision"
CATEGORY_FEEDBACK   = "figma_feedback"

FIGMA_BASE_URL = "https://api.figma.com/v1"


def _hex(r: float, g: float, b: float) -> str:
    """Convert Figma's 0-1 RGBA floats to hex string."""
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


class FigmaAdapter(CognitiveAdapter):
    """Persistent memory + intelligence layer for Figma.

    Wraps the Cognitive Fabric to store and recall Figma design tokens,
    typography, color palettes, component counts, and style decisions
    across sessions. Also translates design semantics into cross-tool
    recommendations (e.g. what voice suits this design in ElevenLabs).

    Example::

        fabric = get_fabric(SQLiteMemoryBackend("agent.db"))
        figma = FigmaAdapter(fabric)

        # One-time sync from the real Figma file
        figma.sync(file_key="abc123", access_token="figd_...")

        # From now on, recall without hitting Figma API
        tokens = figma.recall()

        # Ask what voice ElevenLabs should use based on this design
        rec = figma.recommend(target_tool="elevenlabs")
    """

    def __init__(self, fabric: Any) -> None:
        if hasattr(fabric, "backend"):
            self.fabric = fabric
            self.backend = self.fabric.backend
        else:
            from cognicore.fabric.registry import get_fabric
            self.fabric = get_fabric(fabric)
            self.backend = fabric
        super().__init__(self.fabric)

    # ── CognitiveAdapter interface ────────────────────────────────────────────

    def observe(self, action: str, context: Dict[str, Any]) -> str:
        return self.fabric.record_observation("figma", action, context)

    def learn(self, **kwargs) -> str:
        return self.learn_from_design(**kwargs)

    def feedback(self, action_id: str, success_score: float, **kwargs) -> None:
        self._store_entry(
            category=CATEGORY_FEEDBACK,
            text=f"Figma design feedback: score={success_score:.2f}",
            memory_type="feedback",
            confidence=success_score,
            metadata={"action_id": action_id, "score": success_score, **kwargs},
        )

    def recommend(self, target_tool: str = "elevenlabs", **kwargs) -> Dict[str, Any]:
        """Get cross-tool recommendations derived from stored Figma design tokens.

        Asks the Cognitive Fabric to translate the current design concept
        into instructions for the target tool (ElevenLabs, Cursor, Claude, etc.)

        Args:
            target_tool: Which tool to generate recommendations for.
                         One of: "elevenlabs", "cursor", "claude".

        Returns:
            Dict with tool-specific recommendations and reasoning.
        """
        fabric_rec = self.fabric.translate_for_tool(target_tool, kwargs)
        if fabric_rec:
            return fabric_rec
        # Fall back to local analysis
        tokens = self.recall()
        return self._local_recommend(tokens, target_tool)

    # ── Tier 1: Sync from Figma REST API ─────────────────────────────────────

    def sync(
        self,
        file_key: str,
        access_token: str,
        include_variables: bool = True,
        include_styles: bool = True,
        include_components: bool = True,
    ) -> Dict[str, Any]:
        """Fetch real data from the Figma REST API and store in CogniCore memory.

        Hits the following Figma endpoints:
          GET /v1/files/:key              — file structure, name, last modified
          GET /v1/files/:key/styles       — published styles (colors, text, effects)
          GET /v1/files/:key/components   — published components
          GET /v1/files/:key/variables/local — local variables (design tokens)

        Stores results persistently. Future recall() calls need no API token.

        Args:
            file_key: The Figma file key from the URL
                      (figma.com/file/<KEY>/...).
            access_token: Figma Personal Access Token.
                          Get one at figma.com/settings -> Security.
            include_variables: Fetch design variables (tokens). Default True.
            include_styles: Fetch published styles. Default True.
            include_components: Fetch published components. Default True.

        Returns:
            Dict with status, file name, and summary of what was stored.
        """
        headers = {"X-Figma-Token": access_token}
        stored: Dict[str, int] = {}
        file_name = file_key

        try:
            import requests
        except ImportError:
            return {"status": "error", "message": "pip install requests"}

        # ── 1. File metadata ──────────────────────────────────────────────────
        try:
            resp = requests.get(f"{FIGMA_BASE_URL}/files/{file_key}", headers=headers, timeout=15)
            resp.raise_for_status()
            file_data = resp.json()
            file_name = file_data.get("name", file_key)
            last_modified = file_data.get("lastModified", "")
            thumbnail_url = file_data.get("thumbnailUrl", "")

            # Extract background color from the first page
            bg_color = ""
            pages = file_data.get("document", {}).get("children", [])
            if pages:
                bg = pages[0].get("backgroundColor", {})
                if bg:
                    bg_color = _hex(bg.get("r", 1), bg.get("g", 1), bg.get("b", 1))

            # Extract all text style fonts used
            fonts_used: List[str] = []
            def _walk(node: dict):
                if node.get("type") == "TEXT":
                    font = node.get("style", {}).get("fontFamily", "")
                    if font and font not in fonts_used:
                        fonts_used.append(font)
                for child in node.get("children", []):
                    _walk(child)
            for page in pages:
                _walk(page)

            self._clear_category(CATEGORY_FILE)
            self._store_entry(
                category=CATEGORY_FILE,
                text=f"Figma file: {file_name} (key={file_key})",
                memory_type="semantic",
                confidence=1.0,
                metadata={
                    "file_key": file_key,
                    "file_name": file_name,
                    "last_modified": last_modified,
                    "thumbnail_url": thumbnail_url,
                    "background_color": bg_color,
                    "fonts_used": fonts_used,
                    "page_count": len(pages),
                },
            )
            stored["file"] = 1
            logger.info(f"[Figma] Synced file metadata: {file_name}")
        except Exception as e:
            logger.warning(f"[Figma] File fetch failed: {e}")
            stored["file"] = 0

        # ── 2. Styles (colors, typography, effects) ───────────────────────────
        if include_styles:
            try:
                resp = requests.get(
                    f"{FIGMA_BASE_URL}/files/{file_key}/styles",
                    headers=headers, timeout=15
                )
                resp.raise_for_status()
                styles = resp.json().get("meta", {}).get("styles", [])

                colors, typography, effects = [], [], []
                for s in styles:
                    stype = s.get("style_type", "")
                    name  = s.get("name", "")
                    desc  = s.get("description", "")
                    if stype == "FILL":
                        colors.append({"name": name, "description": desc})
                    elif stype == "TEXT":
                        typography.append({"name": name, "description": desc})
                    elif stype == "EFFECT":
                        effects.append({"name": name, "description": desc})

                self._clear_category(CATEGORY_STYLES)
                self._store_entry(
                    category=CATEGORY_STYLES,
                    text=f"Figma styles: {len(colors)} colors, {len(typography)} text styles, {len(effects)} effects",
                    memory_type="semantic",
                    confidence=1.0,
                    metadata={
                        "colors": colors,
                        "typography": typography,
                        "effects": effects,
                        "total": len(styles),
                    },
                )
                stored["styles"] = len(styles)
                logger.info(f"[Figma] Synced {len(styles)} styles")
            except Exception as e:
                logger.warning(f"[Figma] Styles fetch failed: {e}")

        # ── 3. Components ─────────────────────────────────────────────────────
        if include_components:
            try:
                resp = requests.get(
                    f"{FIGMA_BASE_URL}/files/{file_key}/components",
                    headers=headers, timeout=15
                )
                resp.raise_for_status()
                components = resp.json().get("meta", {}).get("components", [])

                component_names = [c.get("name", "") for c in components[:50]]
                self._clear_category(CATEGORY_COMPONENTS)
                self._store_entry(
                    category=CATEGORY_COMPONENTS,
                    text=f"Figma components: {len(components)} published components",
                    memory_type="semantic",
                    confidence=1.0,
                    metadata={
                        "count": len(components),
                        "names": component_names,
                    },
                )
                stored["components"] = len(components)
                logger.info(f"[Figma] Synced {len(components)} components")
            except Exception as e:
                logger.warning(f"[Figma] Components fetch failed: {e}")

        # ── 4. Variables / Design tokens ──────────────────────────────────────
        if include_variables:
            try:
                resp = requests.get(
                    f"{FIGMA_BASE_URL}/files/{file_key}/variables/local",
                    headers=headers, timeout=15
                )
                resp.raise_for_status()
                var_data = resp.json().get("meta", {})
                variables  = var_data.get("variables", {})
                collections = var_data.get("variableCollections", {})

                # Flatten to name:value pairs for storage
                tokens: Dict[str, Any] = {}
                for var_id, var in variables.items():
                    name  = var.get("name", var_id)
                    vtype = var.get("resolvedType", "")
                    # Take first mode value
                    modes = var.get("valuesByMode", {})
                    value = next(iter(modes.values()), None) if modes else None
                    if value is not None:
                        if vtype == "COLOR" and isinstance(value, dict):
                            value = _hex(value.get("r", 0), value.get("g", 0), value.get("b", 0))
                        tokens[name] = {"type": vtype, "value": value}

                self._clear_category(CATEGORY_VARIABLES)
                self._store_entry(
                    category=CATEGORY_VARIABLES,
                    text=f"Figma design tokens: {len(tokens)} variables across {len(collections)} collections",
                    memory_type="semantic",
                    confidence=1.0,
                    metadata={
                        "tokens": dict(list(tokens.items())[:100]),  # cap at 100
                        "total": len(tokens),
                        "collections": len(collections),
                    },
                )
                stored["variables"] = len(tokens)
                logger.info(f"[Figma] Synced {len(tokens)} design tokens")
            except Exception as e:
                logger.warning(f"[Figma] Variables fetch failed: {e}")

        total = sum(stored.values())
        logger.info(f"[Figma] Sync complete for '{file_name}': {total} items stored")

        return {
            "status": "success",
            "file_name": file_name,
            "file_key": file_key,
            "stored": stored,
            "message": f"Synced '{file_name}': {total} Figma items stored in CogniCore memory.",
        }

    # ── Tier 2: Recall stored tokens ─────────────────────────────────────────

    def recall(self) -> Dict[str, Any]:
        """Retrieve stored Figma design tokens from CogniCore memory.

        Works across sessions — no Figma API token needed after first sync().

        Returns:
            Dict with file info, colors, typography, variables, components.
            Returns empty defaults if nothing has been synced yet.
        """
        result: Dict[str, Any] = {
            "synced": False,
            "file_name": "",
            "file_key": "",
            "background_color": "",
            "fonts_used": [],
            "page_count": 0,
            "styles": {"colors": [], "typography": [], "effects": []},
            "variables": {},
            "components": {"count": 0, "names": []},
        }

        file_entries = self.backend.get_by_category(CATEGORY_FILE, top_k=1)
        if file_entries:
            m = file_entries[0].metadata or {}
            result["synced"] = True
            result["file_name"]        = m.get("file_name", "")
            result["file_key"]         = m.get("file_key", "")
            result["background_color"] = m.get("background_color", "")
            result["fonts_used"]       = m.get("fonts_used", [])
            result["page_count"]       = m.get("page_count", 0)
            result["last_modified"]    = m.get("last_modified", "")
            result["thumbnail_url"]    = m.get("thumbnail_url", "")

        style_entries = self.backend.get_by_category(CATEGORY_STYLES, top_k=1)
        if style_entries:
            m = style_entries[0].metadata or {}
            result["styles"] = {
                "colors":     m.get("colors", []),
                "typography": m.get("typography", []),
                "effects":    m.get("effects", []),
            }

        var_entries = self.backend.get_by_category(CATEGORY_VARIABLES, top_k=1)
        if var_entries:
            m = var_entries[0].metadata or {}
            result["variables"] = m.get("tokens", {})
            result["variable_count"] = m.get("total", 0)

        comp_entries = self.backend.get_by_category(CATEGORY_COMPONENTS, top_k=1)
        if comp_entries:
            m = comp_entries[0].metadata or {}
            result["components"] = {
                "count": m.get("count", 0),
                "names": m.get("names", []),
            }

        return result

    # ── Tier 3: Learning ──────────────────────────────────────────────────────

    def learn_from_design(
        self,
        component: str,
        change: str,
        outcome: str,
        success: bool = True,
        metric: str = "",
        metric_value: float = 0.0,
    ) -> str:
        """Record a design decision and its outcome for future recommendations.

        Over time, these entries let CogniCore learn which design choices
        correlate with positive outcomes (engagement, conversion, etc.)

        Args:
            component: Which Figma component was changed (e.g. "hero_section").
            change: Description of the change (e.g. "increased padding 16->32px").
            outcome: What happened (e.g. "bounce_rate_down_12pct").
            success: Was this a positive outcome?
            metric: Optional metric name (e.g. "conversion_rate").
            metric_value: Optional numeric metric value.

        Returns:
            The stored entry ID.
        """
        confidence = 0.9 if success else 0.3
        text = (
            f"Design decision: changed '{component}' ({change}). "
            f"Outcome: {outcome}. Success: {success}."
        )
        return self._store_entry(
            category=CATEGORY_DECISION,
            text=text,
            memory_type="procedural",
            confidence=confidence,
            metadata={
                "component": component,
                "change": change,
                "outcome": outcome,
                "success": success,
                "metric": metric,
                "metric_value": metric_value,
                "timestamp": time.time(),
            },
        )

    def learn_from_component_usage(
        self,
        component_name: str,
        usage_count: int,
        screens: List[str],
        is_core: bool = False,
    ) -> str:
        """Record which components are used most across the design system.

        Args:
            component_name: Name of the Figma component.
            usage_count: How many times it appears in the file.
            screens: List of screen/page names where it appears.
            is_core: Whether this is a foundational component.

        Returns:
            The stored entry ID.
        """
        text = (
            f"Figma component '{component_name}' used {usage_count} times "
            f"across {len(screens)} screens."
        )
        return self._store_entry(
            category=CATEGORY_COMPONENTS,
            text=text,
            memory_type="semantic",
            confidence=0.95,
            metadata={
                "component_name": component_name,
                "usage_count": usage_count,
                "screens": screens,
                "is_core": is_core,
            },
        )

    # ── Tier 4: Design history ────────────────────────────────────────────────

    def get_design_decisions(self, successful_only: bool = False) -> List[Dict]:
        """Retrieve stored design decisions and outcomes.

        Args:
            successful_only: If True, only return decisions where success=True.

        Returns:
            List of design decision dicts, sorted by confidence.
        """
        entries = self.backend.get_by_category(CATEGORY_DECISION, top_k=100)
        decisions = []
        for entry in entries:
            meta = entry.metadata or {}
            if successful_only and not meta.get("success", True):
                continue
            decisions.append({
                "component":    meta.get("component", ""),
                "change":       meta.get("change", ""),
                "outcome":      meta.get("outcome", ""),
                "success":      meta.get("success", True),
                "confidence":   entry.confidence,
                "metric":       meta.get("metric", ""),
                "metric_value": meta.get("metric_value", 0.0),
            })
        decisions.sort(key=lambda d: d["confidence"], reverse=True)
        return decisions

    def get_design_concept(self) -> Dict[str, Any]:
        """Derive the high-level design concept from stored tokens.

        Returns a concept dict (e.g. {"concept": "Minimalist", "confidence": 0.95})
        that the Fabric uses to generate cross-tool recommendations.
        """
        tokens = self.recall()
        return self._derive_concept(tokens)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _store_entry(
        self,
        category: str,
        text: str,
        memory_type: str = "semantic",
        confidence: float = 1.0,
        metadata: Optional[Dict] = None,
    ) -> str:
        entry = MemoryEntry(
            text=text,
            category=category,
            memory_type=memory_type,
            confidence=confidence,
            scope=MemoryScope.GLOBAL,
            metadata=metadata or {},
        )
        return str(self.backend.store(entry))

    def _clear_category(self, category: str) -> None:
        try:
            entries = self.backend.get_by_category(category, top_k=1000)
            for e in entries:
                if hasattr(self.backend, "delete") and e.entry_id:
                    try:
                        self.backend.delete(e.entry_id)
                    except Exception:
                        pass
        except Exception:
            pass

    def _derive_concept(self, tokens: Dict[str, Any]) -> Dict[str, Any]:
        """Score the design along a calm/bold axis from stored tokens."""
        score = 0

        bg = tokens.get("background_color", "")
        if bg:
            try:
                # Light backgrounds (#F0+) score calm
                r = int(bg[1:3], 16) if len(bg) == 7 else 128
                if r >= 240:
                    score += 2
                elif r >= 200:
                    score += 1
            except Exception:
                pass

        # Variables scan for spacing / radius hints
        for name, var in tokens.get("variables", {}).items():
            if isinstance(var, dict):
                v = var.get("value", "")
                name_l = name.lower()
                if "spacing" in name_l or "padding" in name_l:
                    try:
                        if float(str(v).replace("px", "")) >= 24:
                            score += 1
                    except Exception:
                        pass
                if "radius" in name_l or "corner" in name_l:
                    try:
                        if float(str(v).replace("px", "")) >= 12:
                            score += 1
                    except Exception:
                        pass

        if tokens.get("styles", {}).get("effects"):
            score -= 1  # shadows/effects = less minimalist

        concept = "Minimalist" if score >= 2 else "Bold"
        confidence = min(0.55 + score * 0.1, 0.98)
        return {"concept": concept, "confidence": round(confidence, 2), "score": score}

    def _local_recommend(self, tokens: Dict[str, Any], target_tool: str) -> Dict[str, Any]:
        """Generate recommendations for target_tool from stored design tokens."""
        concept = self._derive_concept(tokens)
        c = concept["concept"]

        if target_tool == "elevenlabs":
            if c == "Minimalist":
                return {
                    "voice": "Rachel", "speed": 0.85, "stability": 0.80,
                    "style": "warm and calm",
                    "reason": f"'{c}' design ({concept['confidence']:.0%} confidence) -> calm, measured voice.",
                    "concept": c,
                }
            return {
                "voice": "Adam", "speed": 1.05, "stability": 0.65,
                "style": "energetic and clear",
                "reason": f"'{c}' design -> high-energy, confident voice.",
                "concept": c,
            }

        if target_tool in ("cursor", "claude"):
            if c == "Minimalist":
                return {
                    "instructions": [
                        f"Background: {tokens.get('background_color', '#F9F9F9')} — light, clean.",
                        f"Fonts: {', '.join(tokens.get('fonts_used', ['Inter']))or 'Inter'}.",
                        "Padding: generous (32px+). One action per card.",
                        "Animations: subtle fades (200ms max). No flashy effects.",
                        "Color palette: muted, desaturated. Avoid neons.",
                    ],
                    "reason": f"Derived from '{tokens.get('file_name', 'Figma')}' ({c} design).",
                    "concept": c,
                }
            return {
                "instructions": [
                    "High contrast layouts, bold visual hierarchy.",
                    "Strong accent colors. Dark or vivid backgrounds.",
                    "Dense information layouts acceptable.",
                    "Animations welcome: slide-in, scale effects.",
                ],
                "reason": f"Derived from '{tokens.get('file_name', 'Figma')}' ({c} design).",
                "concept": c,
            }

        return {"concept": c, "confidence": concept["confidence"], "target_tool": target_tool}

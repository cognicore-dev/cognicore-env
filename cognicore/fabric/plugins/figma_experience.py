"""
CogniCore Figma Experience Layer
==================================
This is the second, more powerful layer of the Figma integration.

Layer 1 (figma.py)      — REST API: reads design tokens and stores them.
Layer 2 (this file)     — Experience: remembers HOW the team implemented
                          each Figma component, so agents never duplicate work.

The killer flow:
    Session 1:
        Agent sees Figma "Button/Primary" → creates Button.tsx
        CogniCore records: figma:Button/Primary → src/components/Button.tsx

    Session 2:
        Agent sees Figma "Button/Primary" again
        CogniCore recalls: "Implemented before → reuse Button.tsx"
        Agent reuses instead of creating a duplicate.

Three memory types used:
    episodic    — "We implemented Button/Primary on 2024-08-10"
    procedural  — "To implement a Figma component, always check CogniCore first"
    semantic    — "Button/Primary maps to src/components/Button.tsx"

Also includes:
    FigmaWebhookReceiver — FastAPI endpoint for live Figma change events

Usage:

    from cognicore.fabric.plugins.figma_experience import FigmaExperienceAdapter

    exp = FigmaExperienceAdapter(fabric)

    # After an agent implements a Figma component:
    exp.record_implementation(
        figma_component="Button/Primary",
        figma_node_id="123:456",
        code_file="src/components/Button.tsx",
        notes="Uses shadcn Button base, adds brand color override",
    )

    # Before agent starts implementing a new component:
    rec = exp.check_before_implement("Button/Primary")
    # -> {"already_implemented": True, "code_file": "src/components/Button.tsx",
    #     "notes": "Uses shadcn Button base...", "recommendation": "REUSE"}

    # Get full design system knowledge:
    ds = exp.get_design_system()
    # -> {"components": [...], "conventions": [...], "mistakes": [...]}
"""

import json
import logging
import time
import hashlib
from typing import Any, Dict, List, Optional

from cognicore.memory.base import MemoryEntry, MemoryScope
from cognicore.fabric.base import CognitiveAdapter

logger = logging.getLogger("cognicore.integrations.figma_experience")

# Category constants
CATEGORY_COMPONENT_MAP  = "figma_component_map"   # figma node → code file
CATEGORY_CONVENTION     = "figma_convention"       # project-wide design conventions
CATEGORY_MISTAKE        = "figma_mistake"          # what NOT to do (anti-patterns)
CATEGORY_DESIGN_SYSTEM  = "figma_design_system"    # accumulated system knowledge
CATEGORY_WEBHOOK_EVENT  = "figma_webhook_event"    # live change events from Figma


class FigmaExperienceAdapter(CognitiveAdapter):
    """Experience-layer memory for Figma → Code implementations.

    Stores and retrieves implementation experience so that AI agents
    can learn from previous design-to-code sessions and avoid duplication.

    This is separate from FigmaAdapter (Layer 1) which handles raw design
    tokens. This layer handles higher-level knowledge:
      - Which Figma components have been implemented and where
      - Which conventions the team follows
      - Which mistakes have been made (and corrected)
      - What the design system looks like from an implementation perspective
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
        return self.fabric.record_observation("figma_experience", action, context)

    def learn(self, **kwargs) -> str:
        return self.record_implementation(**kwargs)

    def feedback(self, action_id: str, success_score: float, **kwargs) -> None:
        self._store(
            CATEGORY_COMPONENT_MAP,
            f"Implementation feedback: score={success_score:.2f}",
            "feedback",
            confidence=success_score,
            metadata={"action_id": action_id, "score": success_score, **kwargs},
        )

    def recommend(self, **kwargs) -> Dict[str, Any]:
        component = kwargs.get("figma_component", "")
        if component:
            return self.check_before_implement(component)
        return self.get_design_system()

    # ── Core: Component → Code mapping ───────────────────────────────────────

    def record_implementation(
        self,
        figma_component: str,
        code_file: str,
        figma_node_id: str = "",
        notes: str = "",
        framework: str = "",
        reused_existing: bool = False,
        test_file: str = "",
        verified: bool = False,
    ) -> str:
        """Record that a Figma component has been implemented.

        Call this after an agent successfully implements a Figma component.
        CogniCore will remember this for future sessions.

        Args:
            figma_component: Figma component name, e.g. "Button/Primary".
            code_file: Path to the implementation, e.g. "src/components/Button.tsx".
            figma_node_id: Figma node ID for direct linking (e.g. "123:456").
            notes: Useful context, e.g. "Uses shadcn base, adds brand override".
            framework: Framework used, e.g. "React", "Vue", "Svelte".
            reused_existing: True if agent reused existing code instead of creating new.
            test_file: Path to associated test file.
            verified: True if implementation passed visual regression / tests.

        Returns:
            The stored entry ID.
        """
        action = "reused" if reused_existing else "implemented"
        text = (
            f"Figma component '{figma_component}' {action} → {code_file}. "
            + (f"Notes: {notes}." if notes else "")
        )
        entry_id = self._store(
            CATEGORY_COMPONENT_MAP,
            text,
            "procedural",
            confidence=0.95 if verified else 0.8,
            metadata={
                "figma_component": figma_component,
                "figma_node_id": figma_node_id,
                "code_file": code_file,
                "notes": notes,
                "framework": framework,
                "reused_existing": reused_existing,
                "test_file": test_file,
                "verified": verified,
                "timestamp": time.time(),
            },
        )
        logger.info(f"[FigmaExp] Recorded: {figma_component} → {code_file}")
        return entry_id

    def check_before_implement(self, figma_component: str) -> Dict[str, Any]:
        """Check if a Figma component has already been implemented.

        Call this BEFORE an agent starts implementing a new component.
        Returns the existing implementation if found, or IMPLEMENT if not.

        Args:
            figma_component: Figma component name to check.

        Returns:
            Dict with:
              - already_implemented (bool)
              - recommendation: "REUSE" | "IMPLEMENT" | "UPDATE"
              - code_file (str): path to existing implementation
              - notes (str): implementation context
              - verified (bool): whether it passed tests
        """
        entries = self.backend.get_by_category(CATEGORY_COMPONENT_MAP, top_k=200)
        matches = []
        for entry in entries:
            meta = entry.metadata or {}
            stored_name = meta.get("figma_component", "")
            if stored_name.lower() == figma_component.lower():
                matches.append((entry, meta))

        if not matches:
            # Check mistakes — maybe we know what NOT to do
            mistakes = self._get_mistakes_for(figma_component)
            return {
                "already_implemented": False,
                "recommendation": "IMPLEMENT",
                "figma_component": figma_component,
                "known_mistakes": mistakes,
                "message": f"No existing implementation found for '{figma_component}'. Implement fresh.",
            }

        # Pick highest-confidence match
        matches.sort(key=lambda x: x[0].confidence, reverse=True)
        best_entry, best_meta = matches[0]

        return {
            "already_implemented": True,
            "recommendation": "REUSE" if best_meta.get("verified") else "UPDATE",
            "figma_component": figma_component,
            "code_file": best_meta.get("code_file", ""),
            "test_file": best_meta.get("test_file", ""),
            "notes": best_meta.get("notes", ""),
            "framework": best_meta.get("framework", ""),
            "verified": best_meta.get("verified", False),
            "reused_previously": best_meta.get("reused_existing", False),
            "confidence": best_entry.confidence,
            "message": (
                f"'{figma_component}' already implemented at {best_meta.get('code_file')}. "
                + ("Verified and tested. REUSE it." if best_meta.get("verified")
                   else "Not yet verified — consider reviewing before reusing.")
            ),
        }

    def list_implemented_components(self) -> List[Dict]:
        """List all Figma components with known implementations.

        Returns:
            List of dicts with figma_component, code_file, verified, notes.
        """
        entries = self.backend.get_by_category(CATEGORY_COMPONENT_MAP, top_k=500)
        seen: Dict[str, Dict] = {}
        for entry in entries:
            meta = entry.metadata or {}
            name = meta.get("figma_component", "")
            if not name:
                continue
            if name not in seen or entry.confidence > seen[name]["confidence"]:
                seen[name] = {
                    "figma_component": name,
                    "code_file":       meta.get("code_file", ""),
                    "notes":           meta.get("notes", ""),
                    "verified":        meta.get("verified", False),
                    "framework":       meta.get("framework", ""),
                    "confidence":      entry.confidence,
                }
        return sorted(seen.values(), key=lambda x: x["figma_component"])

    # ── Conventions ───────────────────────────────────────────────────────────

    def record_convention(
        self,
        rule: str,
        category: str = "general",
        example: str = "",
        source: str = "team",
    ) -> str:
        """Record a project-wide design/implementation convention.

        Args:
            rule: The convention, e.g. "Always use 8px spacing increments".
            category: Convention type: "spacing", "naming", "components", etc.
            example: Concrete example of applying the rule.
            source: Where the rule came from: "team", "figma", "codebase".

        Returns:
            The stored entry ID.
        """
        text = f"Design convention [{category}]: {rule}" + (f" Example: {example}" if example else "")
        return self._store(
            CATEGORY_CONVENTION,
            text,
            "semantic",
            confidence=0.9,
            metadata={
                "rule": rule,
                "category": category,
                "example": example,
                "source": source,
                "timestamp": time.time(),
            },
        )

    def get_conventions(self, category: str = "") -> List[Dict]:
        """Retrieve stored project conventions.

        Args:
            category: Filter by category (empty = return all).

        Returns:
            List of convention dicts.
        """
        entries = self.backend.get_by_category(CATEGORY_CONVENTION, top_k=200)
        conventions = []
        for entry in entries:
            meta = entry.metadata or {}
            if category and meta.get("category", "") != category:
                continue
            conventions.append({
                "rule":     meta.get("rule", entry.text),
                "category": meta.get("category", "general"),
                "example":  meta.get("example", ""),
                "source":   meta.get("source", ""),
            })
        return conventions

    # ── Mistakes ──────────────────────────────────────────────────────────────

    def record_mistake(
        self,
        what_happened: str,
        correct_approach: str,
        figma_component: str = "",
        code_context: str = "",
    ) -> str:
        """Record a mistake and the correct approach for future agents.

        Args:
            what_happened: Description of the mistake, e.g.
                           "Created duplicate Button component instead of reusing".
            correct_approach: What should have been done instead.
            figma_component: Which Figma component was involved.
            code_context: Relevant code path or snippet.

        Returns:
            The stored entry ID.
        """
        text = (
            f"Design mistake: {what_happened}. "
            f"Correct approach: {correct_approach}."
        )
        return self._store(
            CATEGORY_MISTAKE,
            text,
            "failure",
            confidence=0.95,
            metadata={
                "what_happened": what_happened,
                "correct_approach": correct_approach,
                "figma_component": figma_component,
                "code_context": code_context,
                "timestamp": time.time(),
            },
        )

    def get_mistakes(self, figma_component: str = "") -> List[Dict]:
        """Retrieve known mistakes and correct approaches.

        Args:
            figma_component: Filter by component (empty = return all).
        """
        entries = self.backend.get_by_category(CATEGORY_MISTAKE, top_k=100)
        mistakes = []
        for entry in entries:
            meta = entry.metadata or {}
            if figma_component and meta.get("figma_component", "") != figma_component:
                continue
            mistakes.append({
                "what_happened":    meta.get("what_happened", ""),
                "correct_approach": meta.get("correct_approach", ""),
                "figma_component":  meta.get("figma_component", ""),
            })
        return mistakes

    # ── Design system view ────────────────────────────────────────────────────

    def get_design_system(self) -> Dict[str, Any]:
        """Full accumulated design-system knowledge from implementation experience.

        Returns a consolidated view of everything CogniCore knows about
        this project's design-to-code mapping.

        Returns:
            Dict with:
              - components: list of all implemented component mappings
              - conventions: list of project conventions
              - mistakes: list of known mistakes and corrections
              - summary: human-readable summary for agent context injection
        """
        components  = self.list_implemented_components()
        conventions = self.get_conventions()
        mistakes    = self.get_mistakes()

        verified_count = sum(1 for c in components if c["verified"])
        summary_lines = [
            f"Design system knowledge: {len(components)} components mapped, "
            f"{verified_count} verified.",
        ]
        if components:
            summary_lines.append(
                "Implemented components: "
                + ", ".join(c["figma_component"] for c in components[:10])
                + ("..." if len(components) > 10 else "")
            )
        if conventions:
            summary_lines.append(
                "Key conventions: "
                + "; ".join(c["rule"] for c in conventions[:5])
            )
        if mistakes:
            summary_lines.append(
                f"Known mistakes to avoid: {len(mistakes)} recorded."
            )

        return {
            "components":   components,
            "conventions":  conventions,
            "mistakes":     mistakes,
            "summary":      " ".join(summary_lines),
            "stats": {
                "total_components": len(components),
                "verified": verified_count,
                "conventions": len(conventions),
                "mistakes": len(mistakes),
            },
        }

    # ── Webhook event ingestion ───────────────────────────────────────────────

    def ingest_webhook_event(self, event: Dict[str, Any]) -> str:
        """Process a Figma webhook event and update memory.

        Call this from the FigmaWebhookReceiver when Figma notifies
        of a file change.

        Figma webhook event types:
            FILE_UPDATE       — file content changed
            FILE_VERSION_UPDATE — new named version saved
            COMMENT           — comment added

        Args:
            event: Raw Figma webhook event payload dict.

        Returns:
            The stored entry ID.
        """
        event_type   = event.get("event_type", "UNKNOWN")
        file_key     = event.get("file_key", "")
        file_name    = event.get("file_name", "")
        triggered_at = event.get("triggered_at", "")
        description  = event.get("description", "")

        text = f"Figma webhook: {event_type} on '{file_name}' ({file_key}) at {triggered_at}."
        if description:
            text += f" Description: {description}"

        entry_id = self._store(
            CATEGORY_WEBHOOK_EVENT,
            text,
            "episodic",
            confidence=1.0,
            metadata={
                "event_type":   event_type,
                "file_key":     file_key,
                "file_name":    file_name,
                "triggered_at": triggered_at,
                "raw_event":    event,
            },
        )
        logger.info(f"[FigmaWebhook] Ingested {event_type} for file '{file_name}'")
        return entry_id

    def get_recent_changes(self, top_k: int = 20) -> List[Dict]:
        """Get recent Figma file change events from webhook history."""
        entries = self.backend.get_by_category(CATEGORY_WEBHOOK_EVENT, top_k=top_k)
        changes = []
        for entry in sorted(entries, key=lambda e: e.timestamp, reverse=True)[:top_k]:
            meta = entry.metadata or {}
            changes.append({
                "event_type":   meta.get("event_type", ""),
                "file_name":    meta.get("file_name", ""),
                "file_key":     meta.get("file_key", ""),
                "triggered_at": meta.get("triggered_at", ""),
            })
        return changes

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _store(
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

    def _get_mistakes_for(self, figma_component: str) -> List[Dict]:
        return [m for m in self.get_mistakes() if m.get("figma_component") == figma_component]

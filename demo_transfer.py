#!/usr/bin/env python3
"""
CogniCore Cross-Tool Memory Transfer — Prototype
=================================================
Prototype only. Scalable later.

What this shows:
  Designer works in Figma
        ↓  (Figma adapter reads the file)
  Fabric stores observations as memory
        ↓  (fabric.transfer() translates + writes)
  ElevenLabs gets voice settings
  Claude/Cursor gets coding instructions

Run:
    python demo_transfer.py
    python demo_transfer.py --figma-key YOUR_KEY --figma-token YOUR_TOKEN
"""

import sys, os, json, argparse, time
from pathlib import Path

sys.path.insert(0, str(Path(r"c:\Users\kaush\OneDrive\Documents\safetymind\cognicore-my-openenv").absolute()))
sys.stdout.reconfigure(encoding="utf-8")

from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
from cognicore.memory.base import MemoryEntry, MemoryScope

# ── Parse args ───────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--figma-key",   default="", help="Figma file key")
parser.add_argument("--figma-token", default="", help="Figma personal access token")
args = parser.parse_args()

# ── Setup persistent memory (survives sessions) ──────────────────────────────
DB = r"c:\Users\kaush\OneDrive\Documents\safetymind\cognicore-my-openenv\cognicore_fabric_transfer.db"
backend = SQLiteMemoryBackend(DB)
backend._init_db()

# ── Inline mini-fabric (no complex imports) ───────────────────────────────────

def store_memory(tool: str, key: str, value: str, confidence: float = 1.0):
    """Write a named memory for a specific tool."""
    entry = MemoryEntry(
        text=f"[{tool}] {key}: {value}",
        category=f"transfer_{tool}",
        memory_type="fabric_observation",
        confidence=confidence,
        scope=MemoryScope.GLOBAL,
        metadata={"tool": tool, "key": key, "value": value}
    )
    backend.store(entry)

def read_memories(tool: str):
    """Read all transferred memories for a tool."""
    try:
        results = backend.search(f"transfer_{tool}", top_k=50, scope=MemoryScope.GLOBAL)
        out = []
        for r in results:
            entry = getattr(r, "entry", r)
            meta = getattr(entry, "metadata", {}) or {}
            if meta.get("tool") == tool:
                out.append(meta)
        return out
    except Exception:
        return []

# ── Step 1: Figma → Fabric ────────────────────────────────────────────────────

def step_figma(file_key: str = "", token: str = "") -> dict:
    """Pull design tokens from Figma (real API or fallback mock)."""
    data = None

    if file_key and token:
        try:
            import requests
            headers = {"X-Figma-Token": token}
            resp = requests.get(f"https://api.figma.com/v1/files/{file_key}", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                print(f"  [Figma] Connected to real file: {data.get('name', file_key)}")
        except Exception as e:
            print(f"  [Figma] API error ({e}), using fallback design data")

    if not data:
        print("  [Figma] Using design data (set --figma-key and --figma-token for real API)")
        data = {
            "name": "CogniCore Design System",
            "styles": {
                "primary_bg": "#F9F9F9",
                "accent":     "#4A90D9",
                "font":       "Inter",
                "radius":     "16px",
                "spacing":    "32px",
                "density":    "low",
            }
        }

    styles = data.get("styles", {})
    # Parse real Figma format if it came from the API
    if "document" in data:
        doc = data["document"]
        for page in doc.get("children", []):
            bg = page.get("backgroundColor", {})
            if bg:
                r, g, b = bg.get("r",1)*255, bg.get("g",1)*255, bg.get("b",1)*255
                styles["primary_bg"] = f"rgb({r:.0f},{g:.0f},{b:.0f})"
            for node in page.get("children", []):
                if node.get("type") == "TEXT":
                    font = node.get("style", {}).get("fontFamily", "")
                    if font:
                        styles["font"] = font

    return styles

# ── Step 2: Fabric — derive semantic concept ──────────────────────────────────

def derive_concept(styles: dict) -> dict:
    """Turn raw design tokens into a universal semantic concept."""
    bg = styles.get("primary_bg", "")
    density = styles.get("density", "medium")
    spacing = styles.get("spacing", "16px")
    radius  = styles.get("radius", "4px")

    # Simple rules — scalable to ML classifier later
    calm_score = 0
    if "F9" in bg or "FA" in bg or "F8" in bg or "ff" in bg.lower() or "247" in bg:
        calm_score += 2   # light/pastel background
    if density == "low":
        calm_score += 2
    if int(spacing.replace("px","") or 16) >= 24:
        calm_score += 1
    if int(radius.replace("px","") or 4) >= 12:
        calm_score += 1

    if calm_score >= 3:
        return {"concept": "Minimalist", "confidence": min(0.6 + calm_score * 0.07, 0.98), "calm_score": calm_score}
    else:
        return {"concept": "Bold", "confidence": 0.75, "calm_score": calm_score}

# ── Step 3: Fabric → ElevenLabs ──────────────────────────────────────────────

ELEVENLABS_TRANSLATIONS = {
    "Minimalist": {
        "voice":       "Rachel",
        "speed":       0.85,
        "stability":   0.80,
        "style":       "warm and calm",
        "pitch":       "low",
    },
    "Bold": {
        "voice":       "Adam",
        "speed":       1.05,
        "stability":   0.65,
        "style":       "energetic and clear",
        "pitch":       "medium",
    },
}

def transfer_to_elevenlabs(concept: dict) -> dict:
    settings = ELEVENLABS_TRANSLATIONS.get(concept["concept"], ELEVENLABS_TRANSLATIONS["Minimalist"])
    # Store in persistent memory
    for k, v in settings.items():
        store_memory("elevenlabs", k, str(v), confidence=concept["confidence"])
    return settings

# ── Step 4: Fabric → Cursor / Claude ─────────────────────────────────────────

CURSOR_TRANSLATIONS = {
    "Minimalist": [
        "Use clean, spacious layouts with generous padding (32px+).",
        "Background color: #F9F9F9 (light, pastel). Avoid dark/heavy themes.",
        "Typography: Inter or similar clean sans-serif. No decorative fonts.",
        "Border-radius: 16px on all cards and inputs.",
        "Avoid flashy animations — use subtle fade (200ms) only.",
        "Keep component density low. One action per card.",
        "Color palette: muted blues and grays. No saturated neons.",
    ],
    "Bold": [
        "Use high-contrast layouts with strong visual hierarchy.",
        "Background: dark (#1A1A2E) or vivid. Bold accent colors.",
        "Typography: heavy weight (700+), impactful headings.",
        "Animations are welcome — use slide-in and scale effects.",
        "Dense information layouts acceptable. Pack value.",
        "Color palette: saturated, vibrant. Brand color prominent.",
    ],
}

def transfer_to_cursor(concept: dict) -> list:
    instructions = CURSOR_TRANSLATIONS.get(concept["concept"], CURSOR_TRANSLATIONS["Minimalist"])
    # Store in persistent memory
    for i, inst in enumerate(instructions):
        store_memory("cursor", f"rule_{i+1}", inst, confidence=concept["confidence"])
    return instructions

# ── Pretty print ──────────────────────────────────────────────────────────────

def line(char="-", n=60):
    print(char * n)

def run():
    print()
    line("=")
    print("  CogniCore Cross-Tool Memory Transfer — Prototype")
    line("=")
    print()

    # Step 1
    print("STEP 1  Figma  ->  Fabric")
    line()
    styles = step_figma(args.figma_key, args.figma_token)
    for k, v in styles.items():
        print(f"  {k:<14}: {v}")

    # Step 2
    print()
    print("STEP 2  Fabric  ->  Semantic Concept")
    line()
    concept = derive_concept(styles)
    print(f"  Detected concept : {concept['concept']}")
    print(f"  Confidence       : {concept['confidence']:.0%}")
    print(f"  Calm score       : {concept['calm_score']}/6")

    # Step 3
    print()
    print("STEP 3  Fabric  ->  ElevenLabs (voice settings)")
    line()
    el_settings = transfer_to_elevenlabs(concept)
    for k, v in el_settings.items():
        print(f"  {k:<14}: {v}")
    print()
    print("  -> Stored as persistent memory in CogniCore.")
    print("  -> ElevenLabs can read these on ANY future session.")

    # Step 4
    print()
    print("STEP 4  Fabric  ->  Cursor / Claude (coding rules)")
    line()
    instructions = transfer_to_cursor(concept)
    for inst in instructions:
        print(f"  - {inst}")
    print()
    print("  -> Stored as persistent memory in CogniCore.")
    print("  -> Cursor MCP will surface these rules automatically.")

    # Verify persistence
    print()
    print("STEP 5  Verify persistence (reading back from DB)")
    line()
    el_mems  = read_memories("elevenlabs")
    cur_mems = read_memories("cursor")
    print(f"  ElevenLabs memories stored : {len(el_mems)}")
    print(f"  Cursor memories stored     : {len(cur_mems)}")
    print(f"  DB location                : {DB}")

    print()
    line("=")
    print(f"  Transfer complete.  Concept: {concept['concept']}  Confidence: {concept['confidence']:.0%}")
    print(f"  {len(el_mems) + len(cur_mems)} memory entries written to persistent store.")
    line("=")
    print()

if __name__ == "__main__":
    run()

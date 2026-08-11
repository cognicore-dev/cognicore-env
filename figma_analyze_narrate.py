#!/usr/bin/env python3
"""
CogniCore: Figma → Analyze → Narrate (ElevenLabs)
===================================================
1. Give a Figma link
2. Deep-reads the whole design: pages, frames, text content, components
3. Groq understands WHAT the design is about
4. Writes a narration script
5. ElevenLabs speaks it → saves MP3

Run:
    python figma_analyze_narrate.py
    python figma_analyze_narrate.py --link "https://www.figma.com/design/..."
"""

import sys, os, re, json, argparse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

FIGMA_TOKEN    = os.environ.get("FIGMA_ACCESS_TOKEN", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
ELEVENLABS_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

parser = argparse.ArgumentParser()
parser.add_argument("--link", default="", help="Full Figma URL")
args = parser.parse_args()

def sep(char="─", n=64): print(char * n)
def header(t): print(); sep("═"); print(f"  {t}"); sep("═"); print()

# ── Extract file key from URL ──────────────────────────────────────────────────
def extract_key(url: str) -> str:
    m = re.search(r'figma\.com/(?:file|design)/([A-Za-z0-9]+)', url)
    return m.group(1) if m else ""

figma_link = args.link
if not figma_link:
    figma_link = input("  Paste Figma link: ").strip()

file_key = extract_key(figma_link)
if not file_key:
    print(f"  ✗ Could not extract file key from: {figma_link}"); sys.exit(1)

print(f"\n  File key: {file_key}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 1 — Deep Figma Analysis
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header("STEP 1  Figma → Deep Analysis")

import requests
headers = {"X-Figma-Token": FIGMA_TOKEN}
resp = requests.get(f"https://api.figma.com/v1/files/{file_key}", headers=headers, timeout=20)
resp.raise_for_status()
data = resp.json()

file_name = data.get("name", "Untitled")
doc       = data.get("document", {})
pages     = doc.get("children", [])

print(f"  File   : {file_name}")
print(f"  Pages  : {len(pages)}")

# ── Walk the node tree and extract everything useful ──────────────────
def walk(node, depth=0, acc=None):
    if acc is None:
        acc = {"texts": [], "frames": [], "components": [], "images": 0,
               "fonts": set(), "colors": set()}
    if depth > 8:   # don't go infinitely deep
        return acc

    ntype = node.get("type", "")
    name  = node.get("name", "")

    if ntype == "TEXT":
        chars = node.get("characters", "").strip()
        if chars and len(chars) > 1:
            acc["texts"].append(chars)
        # Collect font
        style = node.get("style", {})
        font  = style.get("fontFamily", "")
        if font:
            acc["fonts"].add(font)

    elif ntype in ("FRAME", "SECTION", "GROUP", "COMPONENT_SET"):
        if name and name not in ("Group", "Frame"):
            acc["frames"].append(name)

    elif ntype == "COMPONENT":
        if name:
            acc["components"].append(name)

    elif ntype == "RECTANGLE" and node.get("fills"):
        for fill in node.get("fills", []):
            if fill.get("type") == "IMAGE":
                acc["images"] += 1
            elif fill.get("type") == "SOLID":
                c = fill.get("color", {})
                if c:
                    hex_c = "#{:02X}{:02X}{:02X}".format(
                        int(c.get("r",0)*255),
                        int(c.get("g",0)*255),
                        int(c.get("b",0)*255),
                    )
                    acc["colors"].add(hex_c)

    for child in node.get("children", []):
        walk(child, depth+1, acc)

    return acc

# Analyze all pages
all_analysis = {
    "file_name": file_name,
    "pages": [],
}

total_acc = {"texts": [], "frames": [], "components": [], "images": 0,
             "fonts": set(), "colors": set()}

for page in pages:
    page_name = page.get("name", "")
    acc = walk(page)

    total_acc["texts"].extend(acc["texts"])
    total_acc["frames"].extend(acc["frames"])
    total_acc["components"].extend(acc["components"])
    total_acc["images"]     += acc["images"]
    total_acc["fonts"]      |= acc["fonts"]
    total_acc["colors"]     |= acc["colors"]

    all_analysis["pages"].append({
        "name":       page_name,
        "frames":     acc["frames"][:20],
        "texts":      acc["texts"][:30],
        "components": acc["components"][:15],
    })
    print(f"  Page '{page_name}': {len(acc['frames'])} frames, "
          f"{len(acc['texts'])} text nodes, {len(acc['components'])} components")

# Background color from first page
bg = ""
first_page = pages[0] if pages else {}
bg_c = first_page.get("backgroundColor", {})
if bg_c:
    bg = "#{:02X}{:02X}{:02X}".format(
        int(bg_c.get("r",1)*255), int(bg_c.get("g",1)*255), int(bg_c.get("b",1)*255))

# Deduplicate and cap
unique_texts      = list(dict.fromkeys(total_acc["texts"]))[:50]
unique_frames     = list(dict.fromkeys(total_acc["frames"]))[:30]
unique_components = list(dict.fromkeys(total_acc["components"]))[:20]
fonts_list        = sorted(total_acc["fonts"])
colors_list       = sorted(total_acc["colors"])[:10]

print()
print(f"  Total text nodes : {len(unique_texts)}")
print(f"  Total frames     : {len(unique_frames)}")
print(f"  Fonts found      : {', '.join(fonts_list) or 'none'}")
print(f"  Background       : {bg or 'unknown'}")
print(f"  Images           : {total_acc['images']}")

# Store in CogniCore
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
from cognicore.memory.base import MemoryEntry, MemoryScope
from cognicore.fabric.registry import get_fabric
from cognicore.fabric.plugins.figma_experience import FigmaExperienceAdapter

DB      = str(Path(__file__).parent / "cognicore_live_pipeline.db")
backend = SQLiteMemoryBackend(DB)
backend._init_db()
fabric  = get_fabric(backend)
figma_exp = FigmaExperienceAdapter(fabric)

backend.store(MemoryEntry(
    text=f"Figma file '{file_name}': {len(unique_texts)} texts, frames: {', '.join(unique_frames[:10])}",
    category="figma_deep_analysis",
    memory_type="semantic",
    confidence=1.0,
    scope=MemoryScope.GLOBAL,
    metadata={
        "file_key": file_key,
        "file_name": file_name,
        "texts": unique_texts,
        "frames": unique_frames,
        "fonts": fonts_list,
        "colors": colors_list,
        "background": bg,
        "pages": [p["name"] for p in all_analysis["pages"]],
    },
))
print(f"\n  ✓ Analysis stored in CogniCore memory")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 2 — Groq: Understand what the design is about
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header("STEP 2  Groq → Understand the Design")

import openai
groq = openai.OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY)

analysis_dump = {
    "file_name":   file_name,
    "pages":       [p["name"] for p in all_analysis["pages"]],
    "background":  bg,
    "fonts":       fonts_list,
    "colors":      colors_list[:8],
    "frames":      unique_frames[:25],
    "text_content": unique_texts[:40],
    "components":  unique_components[:15],
    "image_count": total_acc["images"],
}

understand_prompt = f"""You are analyzing a Figma design file. Based on the extracted content below,
answer in 2-3 sentences:
1. What is this design/app/product about?
2. Who is it for?
3. What is the main purpose?

Be specific and direct. Use the actual text content as evidence.

Figma file data:
{json.dumps(analysis_dump, indent=2, ensure_ascii=False)}"""

print("  Calling Groq to understand the design...")
understand_resp = groq.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{"role": "user", "content": understand_prompt}],
    max_tokens=250,
    temperature=0.3,
)
what_its_about = understand_resp.choices[0].message.content.strip()
print(f"\n  Design is about:\n  \"{what_its_about}\"\n")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 3 — Groq: Write the narration script
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header("STEP 3  Groq → Write Narration Script")

# Pick tone based on background color
is_dark = bg.startswith("#1") or bg.startswith("#0") or bg.startswith("#2")
tone    = "bold, energetic, and confident" if is_dark else "warm, calm, and clear"
voice   = "Adam" if is_dark else "Rachel"
voice_id = "pNInz6obpgDQGcFmaJgB" if is_dark else "21m00Tcm4TlvDq8ikWAM"

narrate_prompt = f"""Write a professional narration script (60-80 words) for a product walkthrough of this design.

What the design is: {what_its_about}

Requirements:
- Tone: {tone}
- Written to be spoken aloud, not read
- No markdown, no bullet points
- No headers
- Mention the actual product name or purpose from the design
- End with a clear call to action or vision statement
- Sound like a real product demo or pitch voice-over

Write ONLY the script text. Nothing else."""

print("  Writing narration script...")
narrate_resp = groq.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{"role": "user", "content": narrate_prompt}],
    max_tokens=200,
    temperature=0.75,
)
script = narrate_resp.choices[0].message.content.strip()
print(f"\n  Narration Script:\n  {'─'*56}")
for line in script.split("\n"):
    print(f"  {line}")
print(f"  {'─'*56}\n")

# Store script in CogniCore
backend.store(MemoryEntry(
    text=f"Narration script for '{file_name}': {script}",
    category="figma_narration_script",
    memory_type="semantic",
    confidence=0.95,
    scope=MemoryScope.GLOBAL,
    metadata={"script": script, "voice": voice, "tone": tone, "file_key": file_key},
))
print("  ✓ Script stored in CogniCore memory")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 4 — ElevenLabs: Speak the narration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header(f"STEP 4  ElevenLabs → Audio  (Voice: {voice})")

if not ELEVENLABS_KEY:
    print("  ✗ ELEVENLABS_API_KEY not set in .env"); sys.exit(1)

speed     = 1.05 if is_dark else 0.9
stability = 0.65 if is_dark else 0.80

print(f"  Voice    : {voice}")
print(f"  Speed    : {speed}")
print(f"  Stability: {stability}")
print(f"  Script   : {len(script.split())} words\n")
print("  Calling ElevenLabs API...")

el_resp = requests.post(
    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
    headers={
        "xi-api-key": ELEVENLABS_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    },
    json={
        "text": script,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability":        stability,
            "similarity_boost": 0.85,
            "style":            0.2 if is_dark else 0.0,
            "use_speaker_boost": True,
            "speed":            speed,
        },
    },
    timeout=30,
)

if el_resp.status_code == 200:
    safe_name = re.sub(r'[^\w]', '_', file_name)[:30]
    audio_path = Path(__file__).parent / f"narration_{safe_name}.mp3"
    audio_path.write_bytes(el_resp.content)
    size_kb = audio_path.stat().st_size // 1024
    print(f"  ✓ Audio saved: {audio_path}  ({size_kb} KB)")
else:
    print(f"  ✗ ElevenLabs error {el_resp.status_code}:")
    print(f"  {el_resp.text[:300]}")
    sys.exit(1)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Done
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header("DONE")
print(f"  File     : {file_name}")
print(f"  About    : {what_its_about[:100]}...")
print(f"  Voice    : {voice}  |  {len(script.split())} words")
print(f"  Audio    : {audio_path}")
print(f"  Memory   : {DB}")
sep("═")
print()

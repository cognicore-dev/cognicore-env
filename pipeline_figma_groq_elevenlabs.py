#!/usr/bin/env python3
"""
CogniCore Live Pipeline: Figma → Groq → ElevenLabs
====================================================
1. Pulls REAL design tokens from your Figma file (REST API)
2. Feeds them into Groq LLM to generate a design brief
3. Sends that brief to ElevenLabs to produce actual audio narration
4. Stores everything in CogniCore memory — persistent across sessions

Run:
    python pipeline_figma_groq_elevenlabs.py --file-key YOUR_FILE_KEY
    
    # or with ElevenLabs voice:
    python pipeline_figma_groq_elevenlabs.py --file-key YOUR_FILE_KEY --voice rachel
"""

import sys, os, argparse, json
from pathlib import Path
from dotenv import load_dotenv

# Load .env
load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

FIGMA_TOKEN    = os.environ.get("FIGMA_ACCESS_TOKEN", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
ELEVENLABS_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--file-key", default="", help="Figma file key from URL")
parser.add_argument("--voice", default="rachel", help="ElevenLabs voice name")
parser.add_argument("--no-audio", action="store_true", help="Skip ElevenLabs, text only")
args = parser.parse_args()

# ── Setup CogniCore ───────────────────────────────────────────────────────────
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
from cognicore.fabric.registry import get_fabric
from cognicore.fabric.plugins.figma import FigmaAdapter
from cognicore.fabric.plugins.figma_experience import FigmaExperienceAdapter
from cognicore.fabric.plugins.elevenlabs import ElevenLabsAdapter

DB = str(Path(__file__).parent / "cognicore_live_pipeline.db")
backend = SQLiteMemoryBackend(DB)
backend._init_db()
fabric      = get_fabric(backend)
figma_api   = FigmaAdapter(fabric)
figma_exp   = FigmaExperienceAdapter(fabric)
el_adapter  = ElevenLabsAdapter(fabric)

def sep(char="─", n=62): print(char * n)
def header(t): print(); sep("═"); print(f"  {t}"); sep("═")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 1 — Figma REST API → CogniCore
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header("STEP 1  Figma REST API  →  CogniCore Memory")

if not FIGMA_TOKEN:
    print("  ✗ FIGMA_ACCESS_TOKEN not set"); sys.exit(1)

file_key = args.file_key
if not file_key:
    # Try to discover a file from the user's recent files
    import requests
    headers = {"X-Figma-Token": FIGMA_TOKEN}
    me = requests.get("https://api.figma.com/v1/me", headers=headers, timeout=10).json()
    print(f"  Figma user: {me.get('handle')} ({me.get('email')})")
    print("  No --file-key provided. Pass your Figma file key:")
    print("  From URL: figma.com/file/<FILE_KEY>/your-file-name")
    print()
    file_key = input("  File key: ").strip()
    if not file_key:
        print("  No file key given. Exiting."); sys.exit(1)

print(f"  Syncing file: {file_key} ...")
result = figma_api.sync(file_key=file_key, access_token=FIGMA_TOKEN)

if result["status"] != "success":
    print(f"  ✗ Sync failed: {result.get('message')}"); sys.exit(1)

stored = result.get("stored", {})
print(f"  ✓ File: '{result['file_name']}'")
print(f"  ✓ Stored: {stored.get('styles',0)} styles  |  {stored.get('components',0)} components  |  {stored.get('variables',0)} tokens")

tokens  = figma_api.recall()
concept = figma_api.get_design_concept()
print(f"  ✓ Design concept: {concept['concept']} ({concept['confidence']:.0%} confidence)")
print(f"    Background: {tokens.get('background_color','?')}  Fonts: {', '.join(tokens.get('fonts_used',[]) or ['unknown'])}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 2 — Groq LLM generates design brief from tokens
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header("STEP 2  CogniCore Memory  →  Groq LLM  →  Design Brief")

if not GROQ_API_KEY:
    print("  ✗ GROQ_API_KEY not set"); sys.exit(1)

import openai
groq = openai.OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
)

design_summary = {
    "file_name":   tokens.get("file_name", ""),
    "concept":     concept["concept"],
    "confidence":  f"{concept['confidence']:.0%}",
    "background":  tokens.get("background_color", ""),
    "fonts":       tokens.get("fonts_used", []),
    "style_count": len(tokens.get("styles", {}).get("colors", [])),
    "variables":   tokens.get("variable_count", 0),
    "components":  tokens.get("components", {}).get("count", 0),
}

prompt = f"""You are a design director. Based on these Figma design tokens, write a concise 3-sentence 
design brief that captures the visual personality and experience this design system creates.
Be specific about colors, typography, and feel. Make it useful for a voice actor or developer.

Design data:
{json.dumps(design_summary, indent=2)}

Write only the brief. No headers. No bullet points. Just 3 clear sentences."""

print("  Calling Groq (llama-3.3-70b)...")
response = groq.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{"role": "user", "content": prompt}],
    max_tokens=200,
    temperature=0.7,
)
brief = response.choices[0].message.content.strip()
print(f"\n  Design Brief:\n  \"{brief}\"\n")

# Store the brief in CogniCore memory
from cognicore.memory.base import MemoryEntry, MemoryScope
backend.store(MemoryEntry(
    text=f"Design brief for '{tokens.get('file_name','')}': {brief}",
    category="figma_design_brief",
    memory_type="semantic",
    confidence=0.95,
    scope=MemoryScope.GLOBAL,
    metadata={"brief": brief, "file_key": file_key, "concept": concept["concept"]},
))
print("  ✓ Brief stored in CogniCore memory")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 3 — CogniCore recommends ElevenLabs voice based on design
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header("STEP 3  Design Concept  →  ElevenLabs Voice Settings")

el_rec = figma_api.recommend(target_tool="elevenlabs")
voice_name  = el_rec.get("voice", "Rachel")
speed       = el_rec.get("speed", 0.9)
stability   = el_rec.get("stability", 0.75)

print(f"  CogniCore recommends:")
print(f"    Voice    : {voice_name}")
print(f"    Speed    : {speed}")
print(f"    Stability: {stability}")
print(f"    Style    : {el_rec.get('style','')}")
print(f"    Reason   : {el_rec.get('reason','')}")

# Store voice recommendation in ElevenLabs adapter
VOICE_IDS = {
    "rachel": "21m00Tcm4TlvDq8ikWAM",
    "adam":   "pNInz6obpgDQGcFmaJgB",
    "bella":  "EXAVITQu4vr4xnSDxMaL",
    "elli":   "MF3mGyEYCl7XYWbV9V6O",
    "josh":   "TxGEqnHWrfWFTfGW9XjX",
}
voice_id = VOICE_IDS.get(voice_name.lower(), VOICE_IDS["rachel"])

el_adapter.sync(
    voice_id=voice_id,
    voice_name=voice_name,
    stability=stability,
    speed=speed,
    content_type="design_narration",
    tone=el_rec.get("style", "warm and calm"),
)
print(f"  ✓ Voice settings stored in CogniCore")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 4 — ElevenLabs generates actual audio from the brief
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header("STEP 4  Design Brief  →  ElevenLabs Audio")

audio_out = Path(__file__).parent / "output_design_brief.mp3"

if args.no_audio or not ELEVENLABS_KEY:
    if not ELEVENLABS_KEY:
        print("  ℹ  ELEVENLABS_API_KEY not set — skipping audio generation")
        print("     Add your key to .env: ELEVENLABS_API_KEY=your_key")
    else:
        print("  ℹ  --no-audio flag set, skipping")
    print(f"\n  Brief text (ready to paste into ElevenLabs):")
    print(f"  {brief}")
else:
    import requests as req
    print(f"  Calling ElevenLabs (voice={voice_name}, speed={speed})...")
    payload = {
        "text": brief,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": stability,
            "similarity_boost": 0.85,
            "style": 0.0,
            "use_speaker_boost": True,
            "speed": speed,
        },
    }
    el_resp = req.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": ELEVENLABS_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        json=payload,
        timeout=30,
    )
    if el_resp.status_code == 200:
        audio_out.write_bytes(el_resp.content)
        size_kb = audio_out.stat().st_size // 1024
        print(f"  ✓ Audio saved: {audio_out} ({size_kb} KB)")

        # Log usage in ElevenLabs adapter memory
        el_adapter.log_usage(
            voice_used=voice_name,
            content_type="design_narration",
            audio_length_sec=len(brief.split()) / 2.5,  # rough estimate
            success=True,
        )
        print(f"  ✓ Usage logged in CogniCore (ElevenLabs learning)")
    else:
        print(f"  ✗ ElevenLabs error {el_resp.status_code}: {el_resp.text[:200]}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 5 — Groq generates Cursor coding instructions from design
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header("STEP 5  Design Concept  →  Groq  →  Cursor Rules")

cursor_rec = figma_api.recommend(target_tool="cursor")
cursor_prompt = f"""Based on this Figma design brief and coding rules, write a .cursorrules file 
that tells Cursor exactly how to implement components matching this design system.
Be specific. Under 200 words.

Design Brief: {brief}

Base rules:
{json.dumps(cursor_rec.get('instructions', []), indent=2)}

Output only the .cursorrules content."""

print("  Calling Groq for Cursor rules...")
cursor_resp = groq.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{"role": "user", "content": cursor_prompt}],
    max_tokens=300,
    temperature=0.3,
)
cursor_rules = cursor_resp.choices[0].message.content.strip()

# Write actual .cursorrules file
cursorrules_path = Path(__file__).parent / ".cursorrules"
cursorrules_path.write_text(cursor_rules, encoding="utf-8")
print(f"  ✓ .cursorrules written to: {cursorrules_path}")
print(f"\n  Preview:\n  {'─'*50}")
for line in cursor_rules.split("\n")[:8]:
    print(f"  {line}")
print(f"  {'─'*50}")

# Store Cursor rules in CogniCore
figma_exp.record_convention(
    rule=f"Cursor rules derived from Figma '{tokens.get('file_name','')}': {cursor_rules[:200]}",
    category="cursor_rules",
    source="groq+figma",
)
print(f"  ✓ Cursor rules stored in CogniCore memory")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Summary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
header("PIPELINE COMPLETE")
print(f"  File       : {tokens.get('file_name')}")
print(f"  Concept    : {concept['concept']} ({concept['confidence']:.0%})")
print(f"  Voice      : {voice_name} (speed={speed}, stability={stability})")
if not args.no_audio and ELEVENLABS_KEY:
    print(f"  Audio      : {audio_out}")
print(f"  Cursor     : {cursorrules_path}")
print(f"  Memory DB  : {DB}")
print()
print("  Everything is in CogniCore. Next session — no Figma API needed.")
sep("═")
print()

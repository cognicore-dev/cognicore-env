#!/usr/bin/env python3
"""
CogniCore Figma Killer Demo
============================
Shows the full design-to-code experience loop:

    Session 1  — Agent implements Figma components for the first time.
                 CogniCore records every decision.

    Session 2  — New agent, same project. Figma has a new frame.
                 CogniCore recalls past work → prevents duplication.

This is what makes CogniCore genuinely different from "Figma integration."

Run:
    python demo_figma_experience.py
"""

import sys, os, json, time, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))
sys.stdout.reconfigure(encoding="utf-8")

from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
from cognicore.fabric.registry import get_fabric
from cognicore.fabric.plugins.figma_experience import FigmaExperienceAdapter

# Persistent DB — survives across sessions (the whole point)
DB = str(Path(__file__).parent / "cognicore_figma_exp.db")
backend = SQLiteMemoryBackend(DB)
backend._init_db()
fabric = get_fabric(backend)
exp    = FigmaExperienceAdapter(fabric)

def sep(char="-", n=62): print(char * n)
def header(title):
    print(); sep("="); print(f"  {title}"); sep("=")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SESSION 1 — Agent implements for the first time
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

header("SESSION 1 — Agent implements Figma design for first time")

print("""
  FIGMA FRAME RECEIVED:
    Components found:
      - Button/Primary  (node: 10:23)
      - Navbar/Desktop  (node: 10:31)
      - Modal/Confirm   (node: 10:45)
      - Input/Text      (node: 10:52)

  AGENT: Let me check CogniCore before implementing anything...
""")

components_to_implement = [
    ("Button/Primary",  "10:23"),
    ("Navbar/Desktop",  "10:31"),
    ("Modal/Confirm",   "10:45"),
    ("Input/Text",      "10:52"),
]

for component, node_id in components_to_implement:
    check = exp.check_before_implement(component)
    if check["already_implemented"]:
        print(f"  [REUSE]  {component} -> {check['code_file']}")
        print(f"           CogniCore: \"{check['message']}\"")
    else:
        print(f"  [NEW]    {component} -> no existing implementation found")
        print(f"           CogniCore: \"{check['message']}\"")

print("\n  AGENT: All new. Implementing now...\n")

# Simulate agent implementing all four components
implementations = [
    {
        "figma_component": "Button/Primary",
        "figma_node_id":   "10:23",
        "code_file":       "src/components/ui/Button.tsx",
        "framework":       "React",
        "notes":           "Uses shadcn Button base. Brand color override via className prop.",
        "test_file":       "src/components/ui/Button.test.tsx",
        "verified":        True,
    },
    {
        "figma_component": "Navbar/Desktop",
        "figma_node_id":   "10:31",
        "code_file":       "src/components/layout/Navbar.tsx",
        "framework":       "React",
        "notes":           "Sticky positioning. Mobile breakpoint at 768px -> hidden.",
        "test_file":       "",
        "verified":        False,
    },
    {
        "figma_component": "Modal/Confirm",
        "figma_node_id":   "10:45",
        "code_file":       "src/components/ui/ConfirmModal.tsx",
        "framework":       "React",
        "notes":           "Uses Radix Dialog primitive. Accessible. Keyboard-navigable.",
        "test_file":       "src/components/ui/ConfirmModal.test.tsx",
        "verified":        True,
    },
    {
        "figma_component": "Input/Text",
        "figma_node_id":   "10:52",
        "code_file":       "src/components/ui/Input.tsx",
        "framework":       "React",
        "notes":           "Controlled component. Supports error state and helper text.",
        "test_file":       "src/components/ui/Input.test.tsx",
        "verified":        True,
    },
]

for impl in implementations:
    exp.record_implementation(**impl)
    verified_tag = "[verified]" if impl["verified"] else "[unverified]"
    print(f"  [STORED] {impl['figma_component']} -> {impl['code_file']} {verified_tag}")

# Record project conventions discovered during implementation
print()
exp.record_convention("Always use 8px spacing system (multiples of 8)", "spacing",
                       "padding: 16px, margin: 24px, gap: 8px", source="figma")
exp.record_convention("Buttons use rounded corners (border-radius: 8px)", "components",
                       "className='rounded-lg'", source="figma")
exp.record_convention("Never create duplicate UI components — always check CogniCore first",
                       "process", source="team")
exp.record_convention("Dark mode uses CSS variables, not Tailwind dark: prefix",
                       "theming", source="codebase")

# Record a mistake that happened
exp.record_mistake(
    what_happened="Created a new Button component (CustomButton.tsx) without checking CogniCore — duplicate of Button.tsx",
    correct_approach="Always call check_before_implement() before writing any new component.",
    figma_component="Button/Primary",
    code_context="src/components/CustomButton.tsx was deleted after discovery",
)

print("""
  [DONE] Session 1 complete. CogniCore has learned:
         4 component implementations
         4 project conventions
         1 recorded mistake
""")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGMA WEBHOOK — Designer updated the file overnight
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

header("FIGMA WEBHOOK — Designer changed the file overnight")

webhook_event = {
    "event_type":   "FILE_VERSION_UPDATE",
    "file_key":     "abc123xyz",
    "file_name":    "CogniCore Design System",
    "triggered_at": "2026-08-11T03:22:00Z",
    "description":  "Added Dropdown/Select component and updated Button hover state",
}
exp.ingest_webhook_event(webhook_event)

changes = exp.get_recent_changes(top_k=5)
for change in changes:
    print(f"  [{change['event_type']}] {change['file_name']} at {change['triggered_at']}")
    print(f"           Key: {change['file_key']}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SESSION 2 — New agent, new frame, same project
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

header("SESSION 2 — New agent, new Figma frame, same project")

print("""
  NEW FIGMA FRAME RECEIVED (Settings page):
    Components found:
      - Button/Primary    (node: 11:10)   <- EXISTS in codebase
      - Input/Text        (node: 11:18)   <- EXISTS in codebase
      - Dropdown/Select   (node: 11:25)   <- NEW (added by designer overnight)
      - Modal/Confirm     (node: 11:33)   <- EXISTS in codebase

  AGENT: Checking CogniCore before implementing anything...
""")

new_frame_components = [
    ("Button/Primary", "11:10"),
    ("Input/Text",     "11:18"),
    ("Dropdown/Select","11:25"),
    ("Modal/Confirm",  "11:33"),
]

new_to_implement = []
reuse_list       = []

for component, node_id in new_frame_components:
    check = exp.check_before_implement(component)
    if check["already_implemented"]:
        reuse_list.append(check)
        tag = "REUSE" if check["recommendation"] == "REUSE" else "UPDATE"
        print(f"  [{tag}] {component}")
        print(f"         -> {check['code_file']}")
        if check.get("notes"):
            print(f"         Notes: {check['notes']}")
        if check.get("verified"):
            print(f"         Status: verified and tested")
        print()
    else:
        new_to_implement.append(component)
        print(f"  [NEW]  {component} -> implement fresh")
        if check.get("known_mistakes"):
            for m in check["known_mistakes"]:
                print(f"         WARNING: {m['what_happened']}")
                print(f"         Correct: {m['correct_approach']}")
        print()

sep()
print(f"  RESULT: {len(reuse_list)} reused  |  {len(new_to_implement)} to implement")
print(f"  Duplicate components prevented: {len(reuse_list)}")
print()
print("  AGENT: Implementing only Dropdown/Select. Reusing the rest.")
exp.record_implementation(
    figma_component="Dropdown/Select",
    figma_node_id="11:25",
    code_file="src/components/ui/Dropdown.tsx",
    framework="React",
    notes="Radix Select primitive. Matches Button/Primary styling.",
    verified=False,
)
print("  [STORED] Dropdown/Select -> src/components/ui/Dropdown.tsx")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Full design system knowledge view
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

header("COGNICORE DESIGN SYSTEM KNOWLEDGE (full view)")

ds = exp.get_design_system()
print(f"  {ds['summary']}")
print()

print("  COMPONENT MAP:")
for c in ds["components"]:
    tag = "[verified]" if c["verified"] else "[unverified]"
    print(f"    {c['figma_component']:<22} -> {c['code_file']:<40} {tag}")

print()
print("  CONVENTIONS:")
for conv in ds["conventions"]:
    print(f"    [{conv['category']}] {conv['rule']}")

print()
print("  KNOWN MISTAKES:")
for m in ds["mistakes"]:
    print(f"    MISTAKE  : {m['what_happened']}")
    print(f"    CORRECT  : {m['correct_approach']}")

print()
sep("=")
print(f"  DB: {DB}")
sep("=")
print()

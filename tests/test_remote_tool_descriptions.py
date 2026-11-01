"""Tool descriptions must describe the tool, not instruct the model.

The Claude connector directory requires that "tool descriptions contain no
instructions about model behavior, other tools, or external instruction
sources, and no hidden or encoded text". A description that tells the model
when to call something is also a prompt-injection surface, because clients
feed these strings straight to the model.
"""
import asyncio
import re
import unicodedata

from cognicore.connectors import remote

TOOLS = {t.name: t for t in asyncio.run(remote.mcp.list_tools())}

# Phrases that direct the model rather than describe the tool.
INSTRUCTION_PATTERNS = [
    r"\bcall (?:this|it)\b",
    r"\bcalled at\b",
    r"\byou should\b",
    r"\byou must\b",
    r"\bbe sure to\b",
    r"\balways call\b",
    r"\bnever call\b",
    r"\bdon't duplicate\b",
    r"\bignore (?:previous|prior|above)\b",
    r"\bdisregard\b",
    r"\bsystem prompt\b",
]


def _instruction_hits(text):
    return [p for p in INSTRUCTION_PATTERNS if re.search(p, text, re.IGNORECASE)]


def test_descriptions_do_not_instruct_the_model():
    offenders = {n: _instruction_hits(t.description or "") for n, t in TOOLS.items()}
    offenders = {n: hits for n, hits in offenders.items() if hits}
    assert not offenders, f"tool descriptions that instruct the model: {offenders}"


def test_descriptions_do_not_reference_other_tools():
    """Naming another tool makes the directory read it as cross-tool instruction."""
    offenders = {}
    for name, tool in TOOLS.items():
        others = {o for o in TOOLS if o != name and o in (tool.description or "")}
        if others:
            offenders[name] = sorted(others)
    assert not offenders, f"tool descriptions naming other tools: {offenders}"


def test_descriptions_have_no_hidden_or_control_characters():
    """Zero-width and bidi characters can hide text from a human reviewer."""
    offenders = {}
    for name, tool in TOOLS.items():
        bad = {
            f"U+{ord(ch):04X}"
            for ch in (tool.description or "")
            if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs") and ch not in "\n\t"
        }
        if bad:
            offenders[name] = sorted(bad)
    assert not offenders, f"tool descriptions with hidden characters: {offenders}"


def test_every_tool_still_has_a_description():
    missing = [n for n, t in TOOLS.items() if not (t.description or "").strip()]
    assert not missing, f"tools without a description: {missing}"

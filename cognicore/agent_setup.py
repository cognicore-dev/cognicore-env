"""
Teach agents without CogniCore hooks to use CogniCore memory.

    cognicore setup codex             Codex hooks + the memory rule in Codex's AGENTS.md
    cognicore setup codex --no-hooks  only the AGENTS.md rule
    cognicore setup codex --remove    take both out again

Claude Code gets automatic resume and save from the plugin's hooks. Codex only
saw CogniCore as MCP tools, and in a Claude Code -> Codex test it never called
them, so the handoff Claude saved went unused. Setup adds the same hooks for
Codex (``$CODEX_HOME/hooks.json``: SessionStart injects the brief, Stop and
PreCompact save from Codex's session log), and a marked AGENTS.md block that
asks Codex to resume and save, as a fallback until the hooks are trusted.

Re-running replaces CogniCore's hooks and block rather than adding copies; other
hooks and the rest of AGENTS.md are left as they are.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional

BEGIN = "<!-- cognicore:begin -->"
END = "<!-- cognicore:end -->"
_BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)


def _rule() -> str:
    from cognicore.mcp.server import MEMORY_INSTRUCTIONS

    return (f"{BEGIN}\n## CogniCore memory\n\n{MEMORY_INSTRUCTIONS}\n\n"
            "If the cognicore tools are not available, continue without them.\n"
            f"{END}\n")


def codex_home(path: Optional[str] = None) -> Path:
    return Path(path or os.environ.get("CODEX_HOME") or Path.home() / ".codex")


_HOOK_MARK = "-m cognicore.hooks"


def _codex_hooks(python: str) -> Dict[str, list]:
    """CogniCore's Codex hooks: resume at session start, save after each turn and before compaction.

    SessionEnd is not used: Codex gives it 1-3 seconds, too little for a save.
    """
    def handler(args: str, **extra) -> dict:
        command = f'"{python}" {_HOOK_MARK} {args} --agent codex'
        return {"type": "command", "command": command, "commandWindows": command, **extra}

    return {
        "SessionStart": [{"hooks": [handler("session-start", timeout=30, statusMessage="Loading CogniCore memory")]}],
        "Stop": [{"hooks": [handler("save", timeout=60, **{"async": True})]}],
        "PreCompact": [{"hooks": [handler("save", timeout=60)]}],
    }


def _is_ours(group: dict) -> bool:
    return any(_HOOK_MARK in str(h.get("command", "")) for h in group.get("hooks", []) if isinstance(h, dict))


def setup_codex_hooks(home: Optional[str] = None, remove: bool = False, python: Optional[str] = None) -> str:
    """Merge CogniCore's hooks into ``$CODEX_HOME/hooks.json`` (or remove them), keeping all others."""
    target = codex_home(home) / "hooks.json"
    try:
        doc = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except json.JSONDecodeError:
        return f"{target} is not valid JSON; left unchanged. Fix it, then run setup again."
    hooks = doc.setdefault("hooks", {})
    found = 0
    for event in list(hooks):
        kept = [g for g in hooks[event] if not (isinstance(g, dict) and _is_ours(g))]
        found += len(hooks[event]) - len(kept)
        hooks[event] = kept
        if not kept:
            del hooks[event]
    if remove and not found:
        return f"No CogniCore hooks in {target}; nothing to remove."
    if not remove:
        for event, groups in _codex_hooks(python or sys.executable).items():
            hooks.setdefault(event, []).extend(groups)
    if not hooks:
        doc.pop("hooks")
    if not doc and remove:
        if target.exists():
            target.unlink()
        return f"Removed CogniCore hooks ({target} had nothing else and was deleted)."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    if remove:
        return f"Removed CogniCore hooks from {target}."
    return (f"Added CogniCore hooks to {target}.\n"
            "Codex runs a hook only after you trust it: open Codex, run /hooks, and trust the "
            "three CogniCore hooks (once; again only if they change).")


def setup_codex(home: Optional[str] = None, remove: bool = False, hooks: bool = True) -> str:
    """Set up (or remove) CogniCore for Codex: hooks plus the AGENTS.md fallback rule."""
    lines = [setup_codex_hooks(home, remove=remove)] if hooks or remove else []
    lines.append(_setup_agents_md(home, remove))
    return "\n".join(lines)


def _setup_agents_md(home: Optional[str], remove: bool) -> str:
    """Add (or with ``remove``, delete) the CogniCore block in Codex's AGENTS.md. Returns a status line."""
    target = codex_home(home) / "AGENTS.md"
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    without = _BLOCK.sub("", text)
    if remove:
        if without == text:
            return f"No CogniCore block in {target}; nothing to remove."
        # Also drop the blank line setup put before the block.
        target.write_text(without.rstrip("\n") + "\n" if without.strip() else "", encoding="utf-8")
        return f"Removed the CogniCore block from {target}."
    updated = (without.rstrip("\n") + "\n\n" if without.strip() else "") + _rule()
    if updated == text:
        return f"{target} already has the current CogniCore block."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated, encoding="utf-8")
    return (f"Added the CogniCore memory rule to {target}.\n"
            "Codex also needs the CogniCore MCP server, e.g.:\n"
            "  codex mcp add cognicore -- python -m cognicore.mcp.server")

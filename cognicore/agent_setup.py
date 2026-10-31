"""
Teach agents without CogniCore hooks to use CogniCore memory.

    cognicore setup codex            add the memory rule to Codex's user AGENTS.md
    cognicore setup codex --remove   take it out again

Claude Code gets automatic resume and save from the plugin's hooks. Other agents
only see CogniCore as MCP tools, and in a Claude Code -> Codex test Codex never
called them, so the handoff Claude saved went unused. Codex reads
``$CODEX_HOME/AGENTS.md`` (default ``~/.codex/AGENTS.md``) in every session, so
one marked block there makes it resume at the start and save at the end.

The block is delimited by markers, so re-running replaces it rather than
adding a second copy, and anything else in the file is left as it is.
"""

import os
import re
from pathlib import Path
from typing import Optional

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


def setup_codex(home: Optional[str] = None, remove: bool = False) -> str:
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

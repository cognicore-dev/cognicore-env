"""
``cognicore install``: set up automatic CogniCore memory for the agents on this machine.

    pipx install "cognicore-env[mcp]"      # or: uv tool install "cognicore-env[mcp]"
    cognicore install                      # every agent found
    cognicore install --agent codex        # just one
    cognicore install --dry-run            # show the plan, change nothing

- Claude Code: adds the CogniCore marketplace and installs the ``cognicore``
  plugin (local MCP server in lean mode, plus hooks that resume and save
  automatically).
- Codex: installs CogniCore's hooks and AGENTS.md rule (``cognicore setup
  codex``) and registers the local MCP server. An existing ``cognicore``
  server entry is kept. Codex runs the hooks once you trust them with ``/hooks``.

Memory is local (``~/.cognicore/mcp``) and shared by both agents.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

MARKETPLACE_SOURCE = "cognicore-dev/cognicore-env"
MARKETPLACE_NAME = "cognicore-beta-marketplace"
PLUGIN = "cognicore"
LEAN_TOOLSETS = "memory,session"

Step = Tuple[str, Callable[[], str]]


def _run(cmd: List[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"exit code {proc.returncode}")
    return "done"


def _mcp_command() -> List[str]:
    """How an agent should launch the local server: the console script if on PATH, else this Python."""
    script = shutil.which("cognicore-mcp")
    return [script] if script else [sys.executable, "-m", "cognicore.mcp.server"]


def _codex_has_server(home: Path) -> bool:
    config = home / "config.toml"
    return config.exists() and "[mcp_servers.cognicore]" in config.read_text(encoding="utf-8")


def plan(agents: Optional[Sequence[str]] = None, codex_home: Optional[str] = None) -> Tuple[List[Step], List[str]]:
    """Return (steps, notes) for the requested agents (default: every agent found)."""
    from cognicore.agent_setup import codex_home as resolve_codex_home, setup_codex

    wanted = set(agents or ("claude", "codex"))
    steps: List[Step] = []
    notes: List[str] = []

    try:
        import mcp  # noqa: F401
    except ImportError:
        notes.append('The MCP SDK is missing: install with  pipx install "cognicore-env[mcp]"')
    if not shutil.which("cognicore-hooks"):
        notes.append("cognicore-hooks is not on PATH; hooks will fall back to `python -m cognicore.hooks`, "
                     "which needs cognicore in that Python. Installing with pipx or uv tool avoids this.")

    claude = shutil.which("claude")
    if "claude" in wanted:
        if claude:
            steps.append((f"Claude Code: add the {MARKETPLACE_SOURCE} marketplace",
                          lambda: _run([claude, "plugin", "marketplace", "add", MARKETPLACE_SOURCE])))
            steps.append((f"Claude Code: install the {PLUGIN} plugin",
                          lambda: _run([claude, "plugin", "install", f"{PLUGIN}@{MARKETPLACE_NAME}"])))
        else:
            notes.append("Claude Code not found on PATH: skipped. In Claude Code, run "
                         f"/plugin marketplace add {MARKETPLACE_SOURCE}, then /plugin install {PLUGIN}@{MARKETPLACE_NAME}")

    codex = shutil.which("codex")
    home = resolve_codex_home(codex_home)
    if "codex" in wanted:
        if codex or home.exists():
            steps.append(("Codex: install CogniCore hooks and the AGENTS.md rule", lambda: setup_codex(str(home))))
            if _codex_has_server(home):
                notes.append(f"Codex already has a [mcp_servers.cognicore] entry in {home / 'config.toml'}; kept it. "
                             f"For the lean toolset, add COGNICORE_TOOLSETS = \"{LEAN_TOOLSETS}\" to its env.")
            elif codex:
                command = _mcp_command()
                steps.append(("Codex: register the local CogniCore MCP server",
                              lambda: _run([codex, "mcp", "add", "cognicore", "--env",
                                            f"COGNICORE_TOOLSETS={LEAN_TOOLSETS}", "--", *command])))
            else:
                notes.append("Codex CLI not on PATH: register the server later with "
                             f"codex mcp add cognicore --env COGNICORE_TOOLSETS={LEAN_TOOLSETS} -- cognicore-mcp")
        else:
            notes.append("Codex not found: skipped.")
    return steps, notes


def install(agents: Optional[Sequence[str]] = None, dry_run: bool = False,
            codex_home: Optional[str] = None, out=print) -> int:
    steps, notes = plan(agents, codex_home)
    failures = 0
    for description, action in steps:
        if dry_run:
            out(f"  would: {description}")
            continue
        try:
            result = action()
            out(f"  ok:    {description}" + (f"\n         {result}" if result and result != "done" else ""))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            failures += 1
            out(f"  FAIL:  {description}: {e}")
    for note in notes:
        out(f"  note:  {note}")
    if steps and not dry_run:
        out("\nNext: restart Claude Code. In Codex, run /hooks once and trust the three CogniCore hooks.\n"
            f"Memory lives in {os.environ.get('COGNICORE_DATA_DIR') or Path.home() / '.cognicore' / 'mcp'}; "
            "browse it with  python -m cognicore.memory.viewer")
    return 1 if failures else 0

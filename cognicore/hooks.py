"""
Claude Code hooks: automatic memory capture and resume. No LLM calls.

    python -m cognicore.hooks session-start   SessionStart: print this project's resume brief
    python -m cognicore.hooks save            Stop / PreCompact / SessionEnd: save the transcript
    ... [--agent codex]                       which agent's hooks call it (default claude-code)

Claude Code and Codex both pass the hook event as JSON on stdin (session_id,
transcript_path, cwd, source) and accept the same SessionStart output
(hookSpecificOutput.additionalContext). Their transcript formats differ;
cognicore.memory.transcript reads both. Memory goes to the same database as the local MCP server
(``$COGNICORE_DATA_DIR/mcp_memories.db``, default ``~/.cognicore/mcp``), so the
cognicore_* tools see what the hooks saved and the other way round.

Capture is synchronous and rule based: memory is ready as soon as the hook
returns, and costs no model tokens. A hook never fails the session: errors go
to ``hooks.log`` in the data directory and the hook exits 0.

Environment:
    COGNICORE_DATA_DIR       where memory lives
    COGNICORE_AUTO_CAPTURE   "0" disables saving
    COGNICORE_AUTO_RESUME    "0" disables the start-of-session brief
    COGNICORE_RESUME_TOKENS  budget for the brief (default 1000)
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path


def data_dir() -> Path:
    # Same resolution as cognicore.mcp.server._get_data_dir, without importing the MCP SDK.
    raw = os.environ.get("COGNICORE_DATA_DIR", "")
    path = Path(raw) if raw else Path.home() / ".cognicore" / "mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backend():
    from cognicore.memory import SQLiteMemoryBackend

    return SQLiteMemoryBackend(str(data_dir() / "mcp_memories.db"))


def session_start(event: dict, agent: str = "claude-code") -> str:
    """Return the SessionStart hook output, or "" when this project has no saved sessions."""
    if os.environ.get("COGNICORE_AUTO_RESUME") == "0" or not event.get("cwd"):
        return ""
    if event.get("source") in ("resume", "fork"):
        return ""  # the session already carries its own history
    from cognicore.memory.context_preservation import project_key, resume_session

    backend = _backend()
    with backend._get_conn() as conn:
        known = conn.execute("SELECT 1 FROM sessions WHERE project = ? LIMIT 1",
                             (project_key(event["cwd"]),)).fetchone()
    if not known:
        return ""  # nothing to resume: inject nothing, cost nothing
    budget = int(os.environ.get("COGNICORE_RESUME_TOKENS", "1000"))
    brief = resume_session(backend, project=event["cwd"], max_tokens=budget)
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": brief}})


def save(event: dict, agent: str = "claude-code") -> str:
    """Save (or update) the session from its transcript. Returns "" (no output needed)."""
    if os.environ.get("COGNICORE_AUTO_CAPTURE") == "0":
        return ""
    transcript, cwd = event.get("transcript_path"), event.get("cwd") or ""
    if not transcript or not Path(transcript).is_file():
        return ""
    from cognicore.memory.context_preservation import save_session
    from cognicore.memory.transcript import read_transcript

    conversation, supplied = read_transcript(transcript, cwd)
    if not conversation and not supplied:
        return ""
    save_session(
        _backend(), conversation,
        session_name=Path(cwd).name if cwd else "",
        agent=agent, client=f"{agent}-hooks",
        supplied_handoff=supplied or None,
        session_key=f"{agent}:{event.get('session_id', '')}" if event.get("session_id") else "",
        project=cwd,
    )
    return ""


COMMANDS = {"session-start": session_start, "save": save}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else ""
    agent = argv[argv.index("--agent") + 1] if "--agent" in argv[:-1] else "claude-code"
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        handler = COMMANDS.get(command)
        if handler:
            output = handler(event, agent=agent)
            if output:
                sys.stdout.write(output)
    except Exception:  # never break the user's session over memory
        try:
            with open(data_dir() / "hooks.log", "a", encoding="utf-8") as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {command}\n{traceback.format_exc()}\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    COGNICORE_RESUME_TOKENS  budget for the brief (default 700)
    COGNICORE_PROJECT_TOKENS how much of it the curated project context may use (default 300;
                             see cognicore.project_context)
    COGNICORE_CLEAR_AT       context size that triggers the "/clear" tip (default 80000; 0 = off)
    COGNICORE_SYNC           "0" pauses sync with the hosted account (see cognicore.sync)
"""

import json
import os
import re
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
    from cognicore import project_context
    from cognicore.memory.context_preservation import project_key, resume_session

    backend = _backend()
    pointer = _pull_from_other_apps(backend, event["cwd"])
    curated = project_context.load(event["cwd"])  # read first: the source of truth when set
    with backend._get_conn() as conn:
        known = conn.execute("SELECT handoff_json FROM sessions WHERE project = ? LIMIT 1",
                             (project_key(event["cwd"]),)).fetchone()
    if not known and not pointer and project_context.is_empty(curated):
        return ""  # nothing to resume: inject nothing, cost nothing
    budget = int(os.environ.get("COGNICORE_RESUME_TOKENS", "700"))
    if known:
        compression = json.loads(known[0] or "{}").get("compression") or {}
        brief = resume_session(backend, project=event["cwd"], max_tokens=budget, curated=curated)
        if compression.get("method") == "ai":
            # AI summaries can add a few labels while preserving more facts; omit redundant client detail.
            brief = re.sub(r"\nMore available \([^)]*\): [^\n]+", "", brief)
            for decision in compression.get("summary", {}).get("decisions") or []:
                detail = decision.get("why", "")
                if detail and detail not in brief:
                    brief = brief.replace("\n\n=== END CONTEXT", f"\n{detail}\n\n=== END CONTEXT")
            for fact in compression.get("summary", {}).get("key_facts") or []:
                if fact and fact not in brief:
                    brief = brief.replace("\n\n=== END CONTEXT", f"\n{fact}\n\n=== END CONTEXT")
            from cognicore.memory.context_preservation import TokenBudget
            body = re.sub(r"\n\n=== END CONTEXT \| \d+ tokens ===$", "", brief)
            tokens = TokenBudget.estimate_tokens(body)
            brief = f"{body}\n\n=== END CONTEXT | {tokens} tokens ==="
    else:
        brief = project_context.standalone_brief(curated)
    context = "\n\n".join(part for part in (brief, pointer) if part)
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}})


def _pull_from_other_apps(backend, cwd: str) -> str:
    """With sync on, fetch sessions saved in other apps; return a short pointer to the new ones."""
    from cognicore import sync

    if not sync.enabled() or sync.offline():
        return ""  # while the server is unreachable, start from local memory without waiting
    try:
        return sync.pointer_text(sync.pull(backend), cwd)
    except sync.SyncError as e:  # offline or signed out: the session starts as usual
        _log(f"sync pull skipped: {e}")
        sync.note_failure(e)
        return ""


# Stop fires after every reply. Uploading on each one starts a process per turn for a
# handoff that barely changed, so Stop uploads at most this often. SessionEnd and
# PreCompact always upload, and each upload also sends sessions a skipped one left behind.
PUSH_EVERY = 60.0


def _push_in_background(session_key: str, event_name: str = "") -> None:
    from cognicore import sync

    if not session_key or not sync.enabled():
        return
    if event_name == "Stop" and time.time() - float(sync.load_state().get("last_push") or 0) < PUSH_EVERY:
        return
    try:
        sync.push_in_background(session_key)
    except OSError as e:
        _log(f"sync push not started: {e}")


def save(event: dict, agent: str = "claude-code") -> str:
    """Save (or update) the session from its transcript. Returns "" (no output needed)."""
    if os.environ.get("COGNICORE_AUTO_CAPTURE") == "0":
        return ""
    transcript, cwd = event.get("transcript_path"), event.get("cwd") or ""
    if not transcript or not Path(transcript).is_file():
        return ""
    from cognicore.memory.context_preservation import save_session
    from cognicore.memory.handoff import stated_goal
    from cognicore.memory.transcript import read_transcript, start_cwd
    from cognicore.repo import project_folder

    conversation, supplied = read_transcript(transcript, cwd)
    # The event's cwd follows the agent's `cd`; the project is where the session started.
    project = start_cwd(transcript) or cwd
    if not conversation and not supplied:
        return ""
    backend = _backend()
    cwd = work_dir(cwd, conversation)
    project = work_dir(project, conversation)
    session_key = f"{agent}:{event.get('session_id', '')}" if event.get("session_id") else ""
    supplied, compression = _with_ai_summary(backend, conversation, supplied, session_key)
    curated = _project_context(cwd)
    goal, goal_source = "", ""
    if cwd and not supplied.get("goal") and not stated_goal(conversation):
        goal, goal_source = _continued_goal(backend, cwd, session_key, curated), "carried"
    res = json.loads(save_session(
        backend, conversation, goal=goal,
        session_name=project_folder(project).name if project else "",
        agent=agent, client=f"{agent}-hooks",
        supplied_handoff=supplied or None,
        session_key=session_key,
        project=project,
        compact=compression is not None, compression=compression,
        project_context=curated, goal_source=goal_source if goal else "",
    ))
    if not res.get("deduplicated") and not res.get("error"):
        _push_in_background(session_key, str(event.get("hook_event_name") or ""))
    suggested = "" if res.get("deduplicated") else _suggest(backend, res, conversation, cwd, agent, session_key)
    return _join_messages(suggested, clear_hint(transcript, session_key))


CARRY_GOAL_DAYS = 2


def _continued_goal(backend, cwd: str, session_key: str, curated: dict) -> str:
    """The goal of a continuation session ("continue with part 2"), which states none.

    The project context's goal when it has one. Otherwise the goal the project's
    previous session stated, if that session is recent: a goal carried from session
    to session would otherwise describe week-old work forever.
    """
    if (curated or {}).get("goal"):
        return curated["goal"]
    from cognicore.memory.context_preservation import project_key

    with backend._get_conn() as conn:
        row = conn.execute(
            "SELECT handoff_json, timestamp FROM sessions WHERE project = ? AND (external_id IS NULL OR "
            "external_id != ?) AND handoff_json IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
            (project_key(cwd), session_key),
        ).fetchone()
    if not row or time.time() - float(row[1] or 0) > CARRY_GOAL_DAYS * 86400:
        return ""
    doc = json.loads(row[0])
    return "" if doc.get("provenance", {}).get("goal_source") == "carried" else doc.get("goal", "")


def _project_context(cwd: str) -> dict:
    """The project's curated context (.cognicore/context.md), or {} when it has none."""
    try:
        from cognicore import project_context

        ctx = project_context.load(cwd)
        return {} if project_context.is_empty(ctx) else ctx
    except Exception as e:  # the context is optional for a save
        _log(f"project context skipped: {type(e).__name__}: {e}")
        return {}


def work_dir(cwd: str, conversation) -> str:
    """The folder the session worked in: *cwd*, or the one repo below it that the work was in.

    A session started in a parent folder that is not a repo ("safetymind") and then
    worked only inside a repo under it ("safetymind/cognicore-my-openenv/...") is that
    repo's work, and is filed under it.
    """
    if not cwd:
        return cwd
    base = Path(cwd)
    try:
        home = Path.home().resolve()
        # Already in a repo. A repo at the home folder (dotfiles) doesn't count, as in project_context.root.
        if any((p / ".git").exists() for p in (base.resolve(), *base.resolve().parents) if p != home):
            return cwd
        text = " ".join(m.get("content", "") for m in conversation or []
                        if isinstance(m, dict) and isinstance(m.get("content"), str))
        counts: dict = {}
        prefix = str(base).replace("\\", "/").rstrip("/").lower() + "/"
        for m in re.finditer(
                r"[A-Za-z]:[\\/][^\s\"'`)]+|/[^\s\"'`)]+|(?<![\w/.-])[A-Za-z0-9_.-]+[\\/][^\s\"'`)]+",
                text):
            path = m.group(0).replace("\\", "/").rstrip(",.;:")
            rel = path[len(prefix):] if path.lower().startswith(prefix) else path
            top = rel.split("/", 1)[0]
            if top and ":" not in top and (base / top / ".git").exists():
                counts[top] = counts.get(top, 0) + 1
        if counts:
            top, n = max(counts.items(), key=lambda kv: kv[1])
            if n >= 3 and n / sum(counts.values()) >= 0.6:
                return str(base / top)
    except (OSError, ValueError) as e:
        _log(f"work dir check skipped: {type(e).__name__}: {e}")
    return cwd


def _suggest(backend, res: dict, conversation, cwd: str, agent: str, session_key: str) -> str:
    """Propose project-context items the session stated; the one-line message, or ""."""
    if not res.get("session_id") or not cwd:
        return ""
    try:
        from cognicore import project_suggest

        with backend._get_conn() as conn:
            row = conn.execute("SELECT handoff_json FROM sessions WHERE session_id = ?",
                               (res["session_id"],)).fetchone()
        handoff = json.loads(row[0]) if row and row[0] else {}
        return project_suggest.from_session(handoff, conversation, cwd, agent=agent,
                                            session=str(res["session_id"]))
    except Exception as e:  # a suggestion is never worth breaking the save
        _log(f"project suggestions skipped: {type(e).__name__}: {e}")
        return ""


def _join_messages(text: str, hint_json: str) -> str:
    """One Stop-hook output carrying the suggestion line and/or the /clear hint."""
    lines = [text] if text else []
    if hint_json:
        try:
            lines.append(json.loads(hint_json)["systemMessage"])
        except (ValueError, KeyError, TypeError):
            pass
    return json.dumps({"systemMessage": "\n".join(lines)}) if lines else ""


# A session resends its whole history every turn. Past this size, /clear plus
# CogniCore's brief (~1k tokens) continues the same work far cheaper.
DEFAULT_CLEAR_AT = 80_000
CLEAR_STEP = 1.5  # warn again only after the context grows by half


def context_tokens(transcript: str) -> int:
    """Tokens the model received on the latest turn, from the usage Claude Code records
    in its transcript (input + cache reads + cache writes). 0 when not recorded."""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400_000))
            tail = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return 0
    for line in reversed(tail):
        if '"usage"' not in line:
            continue
        try:
            usage = (json.loads(line).get("message") or {}).get("usage") or {}
        except (json.JSONDecodeError, AttributeError):
            continue
        total = sum(int(usage.get(k) or 0) for k in
                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if total:
            return total
    return 0


def clear_hint(transcript: str, session_key: str) -> str:
    """Stop-hook output telling the user the session got expensive, at most once per growth step."""
    raw = os.environ.get("COGNICORE_CLEAR_AT", "")
    limit = int(raw) if raw.isdigit() else DEFAULT_CLEAR_AT
    if limit == 0 or not session_key:
        return ""
    size = context_tokens(transcript)
    if size < limit:
        return ""
    state_file = data_dir() / "clear_hints.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    last = int(state.get(session_key) or 0)
    if last and size < last * CLEAR_STEP:
        return ""
    state[session_key] = size
    try:
        state_file.write_text(json.dumps(dict(list(state.items())[-200:])), encoding="utf-8")
    except OSError:
        pass
    message = (f"CogniCore: this session resends about {size // 1000}k tokens every turn. "
               f"Run /clear to continue the same work from a short (~700-token) CogniCore brief "
               f"(this session is saved). Set COGNICORE_CLEAR_AT=0 to turn this off.")
    return json.dumps({"systemMessage": message})


AI_MODES = {"auto", "always"}


def _with_ai_summary(backend, conversation, supplied: dict, session_key: str):
    """Add an AI summary to the supplied handoff when COGNICORE_AI_COMPRESS is on.

    Calls the model at most once per COGNICORE_AI_MIN_TOKENS of new conversation;
    between calls the session's previous summary is reused, so the Stop hook,
    which fires after every turn, doesn't call the model every turn. Returns
    ``(supplied, compression)``; compression is None when no summary is used.
    """
    mode = os.environ.get("COGNICORE_AI_COMPRESS", "").strip().lower()
    if mode not in AI_MODES:
        return supplied, None
    from cognicore.memory.ai_compress import compress_with_ai
    from cognicore.memory.context_preservation import TokenBudget

    min_tokens = int(os.environ.get("COGNICORE_AI_MIN_TOKENS", "3000"))
    tokens = TokenBudget.estimate_tokens(conversation)
    previous = None
    if session_key:
        with backend._get_conn() as conn:
            row = conn.execute("SELECT handoff_json FROM sessions WHERE external_id = ?", (session_key,)).fetchone()
        if row and row[0]:
            previous = json.loads(row[0]).get("compression")
    if previous and previous.get("summary") and tokens - previous.get("conversation_tokens", 0) < min_tokens:
        info = previous  # not enough new conversation to be worth another call
    elif tokens >= min_tokens or mode == "always":
        summary, info = compress_with_ai(conversation)
        if summary is None:
            _log(f"ai compression skipped: {info.get('skipped')}")
            if not (previous and previous.get("summary")):
                return supplied, None
            info = previous  # keep the last good summary
        else:
            info = {**info, "summary": summary, "conversation_tokens": tokens}
    else:
        return supplied, None

    summary = info["summary"]
    merged = {key: list(summary.get(key) or []) + list(supplied.get(key) or [])
              for key in ("decisions", "open_tasks", "key_facts", "files")}
    for key in ("goal", "current_state"):  # the agent's own handoff, if any, wins
        merged[key] = supplied.get(key) or summary.get(key) or ""
    merged["superseded"] = bool(supplied.get("superseded"))
    return merged, info


def _log(message: str) -> None:
    try:
        with open(data_dir() / "hooks.log", "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


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

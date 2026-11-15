"""
``cognicore usage``: what your agent sessions cost in tokens, and what CogniCore saves.

Reads Claude Code's own transcripts (``~/.claude/projects/*/*.jsonl``), which
record the token usage of every model turn. No model calls, nothing is sent.

Per session it reports:
    startup   tokens the first turn received: system prompt, tools, MCP servers,
              CLAUDE.md and any start-of-session brief, before you typed much
    turns     model turns
    input     input tokens processed over the session (fresh + cache reads + cache writes)
    context   tokens the last turn received: what continuing this session resends every turn

Per project it adds CogniCore's brief size today and the per-turn saving of
continuing after /clear (startup + brief) instead of in the old context.
"""

import argparse
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def transcripts_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def read_session(path: Path) -> Optional[Dict[str, Any]]:
    """Token usage of one Claude Code transcript, or None if it has no model turns."""
    turns: Dict[str, Dict[str, int]] = {}  # message id -> usage (streamed parts repeat the id)
    order: List[str] = []
    cwd, started = "", None
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not cwd and '"cwd"' in line or '"usage"' in line:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = cwd or str(entry.get("cwd") or "")
                    started = started or entry.get("timestamp")
                    msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
                    usage = msg.get("usage")
                    if not isinstance(usage, dict) or entry.get("isSidechain"):
                        continue
                    key = str(msg.get("id") or entry.get("uuid") or len(order))
                    if key not in turns:
                        order.append(key)
                    turns[key] = {k: int(usage.get(k) or 0) for k in USAGE_KEYS + ("output_tokens",)}
    except OSError:
        return None
    sizes = [sum(turns[k][u] for u in USAGE_KEYS) for k in order]
    sizes = [s for s in sizes if s]
    if not sizes:
        return None
    return {
        "session": path.stem, "project": cwd, "started": started, "modified": path.stat().st_mtime,
        "turns": len(sizes), "startup": sizes[0], "context": sizes[-1], "input": sum(sizes),
        "output": sum(turns[k]["output_tokens"] for k in order),
    }


def collect(days: float = 30.0, root: Optional[Path] = None) -> List[Dict[str, Any]]:
    root = root or transcripts_dir()
    since = time.time() - days * 86400
    out = []
    for path in sorted(root.glob("*/*.jsonl")) if root.is_dir() else []:
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        s = read_session(path)
        if s:
            out.append(s)
    return out


def brief_tokens(project: str) -> int:
    """Size of the brief CogniCore would inject for *project* now (0 if it has none)."""
    try:
        from cognicore.hooks import _backend
        from cognicore.memory.context_preservation import TokenBudget, project_key, resume_session
    except ImportError:
        return 0
    backend = _backend()
    with backend._get_conn() as conn:
        if not conn.execute("SELECT 1 FROM sessions WHERE project = ? LIMIT 1", (project_key(project),)).fetchone():
            return 0
    budget = int(os.environ.get("COGNICORE_RESUME_TOKENS", "700"))
    return TokenBudget.estimate_tokens(resume_session(backend, project=project, max_tokens=budget))


def by_project(sessions: Iterable[Dict[str, Any]], with_brief: bool = True) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in sessions:
        groups[s["project"] or "(unknown)"].append(s)
    rows = []
    for project, items in groups.items():
        n = len(items)
        brief = brief_tokens(project) if with_brief and project != "(unknown)" else 0
        avg_context = sum(s["context"] for s in items) // n
        avg_startup = sum(s["startup"] for s in items) // n
        rows.append({
            "project": project, "sessions": n, "turns": sum(s["turns"] for s in items),
            "input": sum(s["input"] for s in items), "output": sum(s["output"] for s in items),
            "avg_startup": avg_startup,
            "avg_context": avg_context, "max_context": max(s["context"] for s in items),
            # After /clear, a turn costs the startup context plus the brief instead of the old context.
            "brief": brief, "saving_per_turn": max(0, avg_context - avg_startup - brief) if brief else 0,
        })
    return sorted(rows, key=lambda r: -r["input"])


def _k(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def render(rows: List[Dict[str, Any]], days: float) -> str:
    if not rows:
        return f"No Claude Code sessions with token usage in the last {days:g} days ({transcripts_dir()})."
    total_in = sum(r["input"] for r in rows)
    total_sessions = sum(r["sessions"] for r in rows)
    startup = sum(r["avg_startup"] * r["sessions"] for r in rows) // total_sessions
    lines = [f"Claude Code, last {days:g} days: {total_sessions} sessions, {sum(r['turns'] for r in rows)} turns, "
             f"{_k(total_in)} input tokens processed.",
             f"Average startup context: {_k(startup)} tokens (system prompt, tools, MCP servers, CLAUDE.md, brief).",
             "", f"{'project':<34} {'sess':>4} {'turns':>5} {'input':>7} {'startup':>7} {'context':>7} "
             f"{'brief':>5} {'saved/turn':>10}"]
    for r in rows[:15]:
        name = Path(r["project"]).name or r["project"]
        lines.append(f"{name[:34]:<34} {r['sessions']:>4} {r['turns']:>5} {_k(r['input']):>7} "
                     f"{_k(r['avg_startup']):>7} {_k(r['avg_context']):>7} {(_k(r['brief']) if r['brief'] else '-'):>5} "
                     f"{(_k(r['saving_per_turn']) if r['saving_per_turn'] else '-'):>10}")
    if len(rows) > 15:
        lines.append(f"... and {len(rows) - 15} more projects (--json for all)")
    lines += ["", "context: what the last turn received, i.e. what continuing that session resends every turn.",
              "saved/turn: context minus (startup + CogniCore's brief): what each turn saves after /clear."]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="cognicore usage", description=__doc__.split("\n\n")[0].strip())
    p.add_argument("--days", type=float, default=30.0, help="look back this many days (default 30)")
    p.add_argument("--json", action="store_true", help="print sessions and projects as JSON")
    args = p.parse_args(argv)
    sessions = collect(args.days)
    rows = by_project(sessions)
    if args.json:
        print(json.dumps({"days": args.days, "projects": rows, "sessions": sessions}, indent=2))
    else:
        print(render(rows, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

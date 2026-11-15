"""
Project context: the curated facts about a project that stay true across sessions.

Session memory is captured automatically from chat, so it is noisy and fades.
Project context is the opposite: a short file that a person writes on purpose
(the goal, the current status, decisions with their reasons, rules, key facts)
and that the start-of-session brief reads first. When a goal is set here, the
brief's goal always comes from this file, never from a guess at a chat sentence.

The file is the only store: ``.cognicore/context.md`` at the repository root
(the folder holding ``.git``; the working folder when there is none). It is
plain Markdown, so it can be edited by hand, reviewed in pull requests and
rolled back with git, and ``cognicore project history`` is its git log::

    cognicore project set goal "Ship Sarvam summaries and fix recall quality"
    cognicore project set status "Summaries live; recall quality next"
    cognicore project add decision "Rule-based stays default" --why "zero cost, local"
    cognicore project add decision "Hosted MCP save per turn" --rejected --why "no session key"
    cognicore project add rule "Never put keys in git"
    cognicore project add fact "Hosted server runs on the Azure VM"
    cognicore project list | remove decision 2 | history [--diff] | path

Secrets are redacted before anything is written.

Environment:
    COGNICORE_PROJECT_TOKENS  most of the brief the project context may use (default 300)
"""

import os
import re
import subprocess  # nosec B404 - only runs git with fixed arguments
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FILE = Path(".cognicore") / "context.md"
DEFAULT_TOKENS = 300
LISTS = ("decisions", "rules", "facts")
TEXTS = ("goal", "status")
KINDS = {"goal": "goal", "status": "status", "decision": "decisions", "decisions": "decisions",
         "rule": "rules", "rules": "rules", "fact": "facts", "facts": "facts"}
_HEADINGS = {"goal": "Goal", "status": "Status", "decisions": "Decisions", "rules": "Rules", "facts": "Facts"}
_INTRO = ("<!-- Project context, curated with `cognicore project` or by hand. CogniCore puts it first in "
          "every new session's brief. Commit it so the team shares it. -->")
_WHY = re.compile(r"^(?P<what>.*?)\s*\(why:\s*(?P<why>.*)\)\s*$", re.I)
_REJECTED = re.compile(r"^\s*rejected\s*:\s*", re.I)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>.*)$")


def root(cwd: Optional[str] = None) -> Path:
    """The repository root containing *cwd* (the nearest folder with ``.git``), else *cwd* itself.

    A repository at the home folder (a dotfiles repo) doesn't count for folders
    inside it, or every project without its own repo would share one file.
    """
    from cognicore.repo import repo_root

    found = repo_root(cwd)
    return found if found is not None else Path(cwd or os.getcwd()).resolve()


def path(cwd: Optional[str] = None) -> Path:
    return root(cwd) / FILE


def empty() -> Dict[str, Any]:
    return {"goal": "", "status": "", "decisions": [], "rules": [], "facts": [], "other": ""}


def parse(text: str) -> Dict[str, Any]:
    """Read the Markdown file. Unknown sections are kept verbatim in ``other`` so hand edits survive."""
    ctx = empty()
    section, other, paragraph = None, [], []

    def flush() -> None:
        if section in TEXTS and paragraph:
            ctx[section] = " ".join(p.strip() for p in paragraph if p.strip())
        paragraph.clear()

    for line in (text or "").splitlines():
        heading = re.match(r"^\s*##\s+(.*?)\s*#*\s*$", line)
        if heading:
            flush()
            section = KINDS.get(heading.group(1).strip().lower())
            if section is None:
                other.append(line)
                section = "other"
            continue
        if section == "other":
            other.append(line)
            continue
        if section is None or re.match(r"^\s*(#\s|<!--)", line):
            continue  # title, intro comment or text before the first section
        if section in TEXTS:
            paragraph.append(line)
            continue
        bullet = _BULLET.match(line)
        if bullet and bullet.group("text").strip():
            ctx[section].append(_item(section, bullet.group("text").strip()))
        elif line.strip() and ctx[section]:  # a wrapped bullet continues the previous item
            last = ctx[section][-1]
            key = "what" if section == "decisions" else "text"
            last[key] = f"{last[key]} {line.strip()}"
    flush()
    ctx["other"] = "\n".join(other).strip()
    return ctx


def _item(section: str, text: str) -> Dict[str, str]:
    if section != "decisions":
        return {"text": text}
    rejected = bool(_REJECTED.match(text))
    text = _REJECTED.sub("", text)
    m = _WHY.match(text)
    what, why = (m.group("what"), m.group("why")) if m else (text, "")
    return {"what": what.strip(), "why": why.strip(), "status": "rejected" if rejected else "made"}


def _decision_line(d: Dict[str, str]) -> str:
    return (("Rejected: " if d.get("status") == "rejected" else "") + d["what"]
            + (f" (why: {d['why']})" if d.get("why") else ""))


def item_text(section: str, item: Dict[str, str]) -> str:
    return _decision_line(item) if section == "decisions" else item["text"]


def dump(ctx: Dict[str, Any]) -> str:
    lines = ["# Project context", "", _INTRO]
    for section in ("goal", "status", *LISTS):
        value = ctx.get(section)
        if not value:
            continue
        lines += ["", f"## {_HEADINGS[section]}", ""]
        if section in TEXTS:
            lines.append(value)
        else:
            lines += [f"- {item_text(section, i)}" for i in value]
    if ctx.get("other"):
        lines += ["", ctx["other"]]
    return "\n".join(lines) + "\n"


def load(cwd: Optional[str] = None) -> Dict[str, Any]:
    """The project's context; empty when there is no file or it can't be read."""
    try:
        return parse(path(cwd).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return empty()


def is_empty(ctx: Dict[str, Any]) -> bool:
    return not any(ctx.get(k) for k in (*TEXTS, *LISTS))


def save(ctx: Dict[str, Any], cwd: Optional[str] = None) -> Path:
    target = path(cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(dump(ctx), encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target


def _clean(text: str) -> Tuple[str, int]:
    from cognicore.memory.redact import redact_secrets

    return redact_secrets(" ".join(str(text or "").split()))


def set_text(kind: str, text: str, cwd: Optional[str] = None) -> Tuple[Dict[str, Any], int]:
    """Set the goal or status. Returns the new context and how many secrets were redacted."""
    ctx = load(cwd)
    ctx[KINDS[kind]], redacted = _clean(text)
    save(ctx, cwd)
    return ctx, redacted


def add(kind: str, text: str, why: str = "", rejected: bool = False,
        cwd: Optional[str] = None) -> Tuple[Dict[str, Any], int, bool]:
    """Add a decision, rule or fact. Returns (context, secrets redacted, added); an exact repeat is not added."""
    section = KINDS[kind]
    ctx = load(cwd)
    text, redacted = _clean(text)
    if section == "decisions":
        why, more = _clean(why)
        redacted += more
        item = {"what": _REJECTED.sub("", text).strip(), "why": why,
                "status": "rejected" if rejected or _REJECTED.match(text) else "made"}
        key = item["what"].lower()
        existing = next((d for d in ctx[section] if d["what"].lower() == key), None)
        if existing:  # same decision again: update its reason and status rather than repeat it
            changed = existing != item
            existing.update(item)
            if changed:
                save(ctx, cwd)
            return ctx, redacted, changed
    else:
        item = {"text": text}
        if any(i["text"].lower() == text.lower() for i in ctx[section]):
            return ctx, redacted, False
    ctx[section].append(item)
    save(ctx, cwd)
    return ctx, redacted, True


def remove(kind: str, number: Optional[int] = None, cwd: Optional[str] = None) -> Optional[str]:
    """Remove the goal/status, or item *number* (1-based, as ``list`` shows it). Returns what was removed."""
    section = KINDS[kind]
    ctx = load(cwd)
    if section in TEXTS:
        gone, ctx[section] = ctx[section], ""
    else:
        items = ctx[section]
        if number is None or not 1 <= number <= len(items):
            return None
        gone = item_text(section, items.pop(number - 1))
    if gone:
        save(ctx, cwd)
    return gone or None


def history(cwd: Optional[str] = None, diff: bool = False) -> str:
    """The git log of the context file: every change, with its date, author and message."""
    target = path(cwd)
    args = ["git", "-C", str(root(cwd)), "log", "--follow", "--date=short",
            "--format=%h %ad %an  %s", *(["-p"] if diff else []), "--", FILE.as_posix()]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)  # nosec B603
    except (OSError, subprocess.SubprocessError) as e:
        return f"Can't read history: {e}"
    if out.returncode != 0 and "does not have any commits" not in (out.stderr or ""):
        return f"Can't read history: {(out.stderr or '').strip() or 'not a git repository'}"
    if out.returncode != 0 or not out.stdout.strip():
        state = "not committed yet" if target.exists() else "doesn't exist yet"
        return f"No history: {FILE.as_posix()} is {state}. Commit it to version it."
    return out.stdout.rstrip()


def format_list(ctx: Dict[str, Any], where: Optional[Path] = None) -> str:
    if is_empty(ctx):
        return ("No project context yet. Start with:\n"
                '  cognicore project set goal "What this project is for"')
    lines = [f"Project context ({where})" if where else "Project context"]
    for section in ("goal", "status", *LISTS):
        value = ctx.get(section)
        if not value:
            continue
        if section in TEXTS:
            lines.append(f"{_HEADINGS[section]}: {value}")
            continue
        lines.append(f"{_HEADINGS[section]}:")
        lines += [f"  {n}. {item_text(section, i)}" for n, i in enumerate(value, 1)]
    return "\n".join(lines)


# --- the start-of-session brief -------------------------------------------------------

def budget() -> int:
    try:
        return max(0, int(os.environ.get("COGNICORE_PROJECT_TOKENS", str(DEFAULT_TOKENS))))
    except ValueError:
        return DEFAULT_TOKENS


def brief_lines(ctx: Dict[str, Any], max_tokens: Optional[int] = None) -> List[str]:
    """The project context as brief lines, within *max_tokens*.

    Order: goal, status, rules (an agent must always see them), then decisions
    newest first, then facts. What doesn't fit is counted with a pointer to the file.
    """
    from cognicore.memory.context_preservation import TokenBudget

    if is_empty(ctx):
        return []
    limit = budget() if max_tokens is None else max_tokens
    head = [f"Project context ({FILE.as_posix()}, curated):"]
    if ctx.get("goal"):
        head.append(f"Goal: {ctx['goal']}")
    if ctx.get("status"):
        head.append(f"Status: {ctx['status']}")
    groups = [("Rules", [i["text"] for i in ctx.get("rules") or []]),
              ("Decisions", [_decision_line(d) for d in reversed(ctx.get("decisions") or [])]),
              ("Facts", [i["text"] for i in ctx.get("facts") or []])]
    lines, left_out = list(head), 0
    used = TokenBudget.estimate_tokens("\n".join(lines))
    for title, items in groups:
        shown = []
        for item in items:
            line = f"- {item}"
            cost = TokenBudget.estimate_tokens(line) + (2 if not shown else 0)
            if used + cost + 12 > limit:  # 12: room for the "more" pointer
                left_out += 1
                continue
            shown.append(line)
            used += cost
        if shown:
            lines += [f"Project {title.lower()}:", *shown]
    if left_out:
        lines.append(f"(+{left_out} more in {FILE.as_posix()}: cognicore project list)")
    return lines


def standalone_brief(ctx: Dict[str, Any], max_tokens: Optional[int] = None) -> str:
    """A brief made of the project context alone, for a project with no saved sessions yet."""
    from cognicore.memory.context_preservation import TokenBudget

    lines = brief_lines(ctx, max_tokens)
    if not lines:
        return ""
    body = "\n".join(["=== COGNICORE CONTEXT RESUME ===", *lines])
    return f"{body}\n\n=== END CONTEXT | {TokenBudget.estimate_tokens(body)} tokens ==="


# --- command line ---------------------------------------------------------------------

def main(argv: List[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="cognicore project",
                                description=f"Curated project context in {FILE.as_posix()}, read first by every new brief.")
    sub = p.add_subparsers(dest="action")
    s = sub.add_parser("set", help="Set the goal or the status")
    s.add_argument("kind", choices=["goal", "status"])
    s.add_argument("text", nargs="+")
    a = sub.add_parser("add", help="Add a decision, rule or fact")
    a.add_argument("kind", choices=["decision", "rule", "fact"])
    a.add_argument("text", nargs="+")
    a.add_argument("--why", default="", help="the reason, for a decision")
    a.add_argument("--rejected", action="store_true", help="a rejected approach, for a decision")
    r = sub.add_parser("remove", help="Remove the goal, the status, or an item by its number in `list`")
    r.add_argument("kind", choices=["goal", "status", "decision", "rule", "fact"])
    r.add_argument("number", nargs="?", type=int)
    sub.add_parser("list", help="Show the project context")
    h = sub.add_parser("history", help="Every change to the file (its git log)")
    h.add_argument("--diff", action="store_true", help="show what each change did")
    sub.add_parser("path", help="Print where the file is")
    args = p.parse_args(argv)

    def note(redacted: int) -> None:
        if redacted:
            print(f"Note: {redacted} secret(s) redacted before saving.")

    action = args.action or "list"
    if action == "set":
        _, redacted = set_text(args.kind, " ".join(args.text))
        print(f"{_HEADINGS[args.kind]} set in {path()}")
        note(redacted)
    elif action == "add":
        if (args.why or args.rejected) and args.kind != "decision":
            p.error("--why and --rejected are for decisions")
        _, redacted, added = add(args.kind, " ".join(args.text), why=args.why, rejected=args.rejected)
        print(f"{args.kind.capitalize()} {'added to' if added else 'already in'} {path()}")
        note(redacted)
    elif action == "remove":
        if args.kind not in TEXTS and args.number is None:
            p.error(f"say which {args.kind}: its number in `cognicore project list`")
        gone = remove(args.kind, args.number)
        if gone is None:
            print(f"Nothing to remove: no {args.kind}" + (f" number {args.number}." if args.number else "."))
            return 1
        print(f"Removed: {gone}")
    elif action == "history":
        print(history(diff=args.diff))
    elif action == "path":
        print(path())
    else:
        print(format_list(load(), path()))
    return 0

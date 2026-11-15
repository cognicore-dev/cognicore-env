"""
Project context: the curated facts about a project that stay true across sessions.

Session memory is captured automatically from chat, so it is noisy and fades.
Project context is the opposite: a short file that a person writes on purpose
(the goal, the current status, decisions with their reasons, rules, key values)
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
    cognicore project add value "Hosted server runs on the Azure VM"
    cognicore project list | remove D2 | history [--diff] | path
    cognicore project suggestions | accept 1 2 | dismiss 3

Each item ends with its provenance in brackets, e.g.
``[id: D3; date: 2026-11-15; by: claude-code; session: sess_...]``. The id is
stable (``remove D3``); items written by hand without one get the next free id.

Suggestions (``accept``) are proposed by the Stop hook from what a session said,
or by an agent through MCP. Only a person accepts them.

Secrets are redacted before anything is written.

Environment:
    COGNICORE_PROJECT_TOKENS  most of the brief the project context may use (default 300)
"""

import getpass
import os
import re
import subprocess  # nosec B404 - only runs git with fixed arguments
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FILE = Path(".cognicore") / "context.md"
DEFAULT_TOKENS = 300
LISTS = ("decisions", "rules", "facts")
TEXTS = ("goal", "status")
KINDS = {"goal": "goal", "status": "status", "decision": "decisions", "decisions": "decisions",
         "rule": "rules", "rules": "rules", "fact": "facts", "facts": "facts", "value": "facts",
         "values": "facts", "key value": "facts", "key values": "facts"}
_HEADINGS = {"goal": "Goal", "status": "Status", "decisions": "Decisions", "rules": "Rules",
             "facts": "Key values"}
PREFIX = {"decisions": "D", "rules": "R", "facts": "V"}
_CORE = {"decisions": ("what", "why", "status"), "rules": ("text",), "facts": ("text",)}
META_ORDER = ("id", "date", "by", "session", "ref", "supersedes", "superseded_by")
_INTRO = ("<!-- Project context, curated with `cognicore project` or by hand. CogniCore puts it first in "
          "every new session's brief. Commit it so the team shares it. -->")
_WHY = re.compile(r"^(?P<what>.*?)\s*\(why:\s*(?P<why>.*)\)\s*$", re.I)
_REJECTED = re.compile(r"^\s*rejected\s*:\s*", re.I)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>.*)$")
# Trailing "[id: D3; date: 2026-11-15; by: claude-code]": only key: value pairs, so a
# bracket that is part of the text ("[WIP]") is left alone.
_META = re.compile(r"\s*\[(?P<meta>\s*[a-z_]+\s*:[^\[\]]*)\]\s*$", re.I)
_REF = re.compile(r"^(?P<prefix>[DRV])(?P<n>\d+)$", re.I)


def root(cwd: Optional[str] = None) -> Path:
    """The repository root containing *cwd* (the nearest folder with ``.git``), else *cwd* itself.

    A repository at the home folder (a dotfiles repo) doesn't count for folders
    inside it, or every project without its own repo would share one file.
    """
    start = Path(cwd or os.getcwd()).resolve()
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None
    for folder in (start, *start.parents):
        if folder == home and folder != start:
            break
        if (folder / ".git").exists():
            return folder
    return start


def path(cwd: Optional[str] = None) -> Path:
    return root(cwd) / FILE


def empty() -> Dict[str, Any]:
    return {"goal": "", "status": "", "decisions": [], "rules": [], "facts": [], "other": ""}


def _parse_meta(raw: str) -> Dict[str, str]:
    meta = {}
    for part in raw.split(";"):
        key, sep, value = part.partition(":")
        key = key.strip().lower()
        if sep and re.fullmatch(r"[a-z_]+", key) and value.strip():
            meta[key] = value.strip()
    return meta


def parse(text: str) -> Dict[str, Any]:
    """Read the Markdown file. Unknown sections are kept verbatim in ``other`` so hand edits survive."""
    ctx = empty()
    section, other, paragraph = None, [], []

    def flush() -> None:
        if section in TEXTS and paragraph:
            ctx[section] = " ".join(p.strip() for p in paragraph if p.strip())
        paragraph.clear()

    raw_items: Dict[str, List[str]] = {k: [] for k in LISTS}
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
            raw_items[section].append(bullet.group("text").strip())
        elif line.strip() and raw_items[section]:  # a wrapped bullet continues the previous item
            raw_items[section][-1] = f"{raw_items[section][-1]} {line.strip()}"
    flush()
    for section in LISTS:
        ctx[section] = [_item(section, t) for t in raw_items[section]]
    ctx["other"] = "\n".join(other).strip()
    _assign_ids(ctx)
    return ctx


def _item(section: str, text: str) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    m = _META.search(text)
    if m:
        meta = _parse_meta(m.group("meta"))
        text = text[:m.start()].rstrip()
    if section != "decisions":
        return {"text": text, **meta}
    rejected = bool(_REJECTED.match(text))
    text = _REJECTED.sub("", text)
    m = _WHY.match(text)
    what, why = (m.group("what"), m.group("why")) if m else (text, "")
    return {"what": what.strip(), "why": why.strip(), "status": "rejected" if rejected else "made", **meta}


def _assign_ids(ctx: Dict[str, Any]) -> None:
    """Give every item a stable id (D1, R1, V1...): kept when present, else the next free number."""
    for section in LISTS:
        prefix = PREFIX[section]
        used = set()
        for item in ctx[section]:
            m = _REF.match(item.get("id", ""))
            if m and m.group("prefix").upper() == prefix and int(m.group("n")) not in used:
                item["id"] = f"{prefix}{int(m.group('n'))}"
                used.add(int(m.group("n")))
            else:
                item.pop("id", None)
        nxt = max(used, default=0)
        for item in ctx[section]:
            if "id" not in item:
                nxt += 1
                item["id"] = f"{prefix}{nxt}"


def meta(item: Dict[str, str], section: str) -> Dict[str, str]:
    """An item's provenance: every key that isn't its text."""
    core = _CORE[section]
    keys = [k for k in META_ORDER if item.get(k)] + sorted(k for k in item if k not in core
                                                           and k not in META_ORDER and item.get(k))
    return {k: str(item[k]) for k in keys}


def _decision_line(d: Dict[str, str]) -> str:
    return (("Rejected: " if d.get("status") == "rejected" else "") + d["what"]
            + (f" (why: {d['why']})" if d.get("why") else ""))


def item_text(section: str, item: Dict[str, str]) -> str:
    """The item as a person reads it, without its provenance."""
    return _decision_line(item) if section == "decisions" else item["text"]


def _with_meta(section: str, item: Dict[str, str]) -> str:
    info = meta(item, section)
    tail = "; ".join(f"{k}: {v}" for k, v in info.items())
    return item_text(section, item) + (f" [{tail}]" if tail else "")


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
            lines += [f"- {_with_meta(section, i)}" for i in value]
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
    _assign_ids(ctx)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(dump(ctx), encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target


def _clean(text: str) -> Tuple[str, int]:
    from cognicore.memory.redact import redact_secrets

    cleaned, redacted = redact_secrets(" ".join(str(text or "").split()))
    return cleaned.replace("[", "(").replace("]", ")") if _META.search(cleaned) else cleaned, redacted


def person() -> str:
    """Who is running the command: git's user.name, else the login name."""
    try:
        out = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True,  # nosec B603 B607
                             timeout=5, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return getpass.getuser()
    except (OSError, KeyError, ImportError):
        return "unknown"


def source(by: str = "", session: str = "", date: str = "") -> Dict[str, str]:
    """Provenance for a new item: today's date, who added it, and the session it came from."""
    info = {"date": date or time.strftime("%Y-%m-%d"), "by": _clean(by)[0] if by else person()}
    if session:
        info["session"] = _clean(session)[0]
    return info


def set_text(kind: str, text: str, cwd: Optional[str] = None) -> Tuple[Dict[str, Any], int]:
    """Set the goal or status. Returns the new context and how many secrets were redacted."""
    ctx = load(cwd)
    ctx[KINDS[kind]], redacted = _clean(text)
    save(ctx, cwd)
    return ctx, redacted


def similar(a: str, b: str) -> float:
    """Key-word overlap of two items (0..1), the measure lessons use to merge repeats."""
    from cognicore.lessons import _similar

    def norm(text: str) -> str:
        return " ".join(re.findall(r"[\w'.-]*\w", str(text or "").lower()))

    return _similar(norm(a), norm(b))


def _same(section: str, a: Dict[str, str], b: Dict[str, str]) -> bool:
    key = "what" if section == "decisions" else "text"
    return " ".join(a[key].lower().split()) == " ".join(b[key].lower().split())


def add(kind: str, text: str, why: str = "", rejected: bool = False, cwd: Optional[str] = None,
        provenance: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, Any], int, bool]:
    """Add a decision, rule or key value. Returns (context, secrets redacted, added); a repeat is not added.

    *provenance* is the item's source (``date``, ``by``, ``session``); by default
    today and the person running the command.
    """
    section = KINDS[kind]
    ctx = load(cwd)
    text, redacted = _clean(text)
    info = dict(provenance) if provenance is not None else source()
    if section == "decisions":
        why, more = _clean(why)
        redacted += more
        item = {"what": _REJECTED.sub("", text).strip(), "why": why,
                "status": "rejected" if rejected or _REJECTED.match(text) else "made"}
        existing = next((d for d in ctx[section] if _same(section, d, item)), None)
        if existing:  # same decision again: update its reason and status rather than repeat it
            changed = any(existing.get(k) != v for k, v in item.items() if v or k != "why")
            if changed:
                existing.update({k: v for k, v in item.items() if v or k != "why"})
                existing.update(info)
                save(ctx, cwd)
            return ctx, redacted, changed
    else:
        item = {"text": text}
        if any(_same(section, i, item) for i in ctx[section]):
            return ctx, redacted, False
    item.update(info)
    ctx[section].append(item)
    save(ctx, cwd)
    return ctx, redacted, True


def find(ref: str, ctx: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    """The (section, item) an id such as ``D3`` names, or (None, None)."""
    m = _REF.match((ref or "").strip())
    if not m:
        return None, None
    section = next(s for s, p in PREFIX.items() if p == m.group("prefix").upper())
    wanted = f"{m.group('prefix').upper()}{int(m.group('n'))}"
    return section, next((i for i in ctx[section] if i.get("id") == wanted), None)


def remove(kind: str, number: Optional[int] = None, cwd: Optional[str] = None) -> Optional[str]:
    """Remove the goal/status, an item by id (``remove("D3")``), or item *number* of *kind* as ``list``
    numbered them before ids. Returns what was removed."""
    ctx = load(cwd)
    if _REF.match(kind or ""):
        section, item = find(kind, ctx)
        if item is None:
            return None
        ctx[section].remove(item)
        save(ctx, cwd)
        return item_text(section, item)
    section = KINDS[kind]
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


def _provenance_note(item: Dict[str, str]) -> str:
    parts = [item.get("date", ""), f"by {item['by']}" if item.get("by") else ""]
    note = ", ".join(p for p in parts if p)
    return f"  ({note})" if note else ""


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
        lines += [f"  {i['id']}. {item_text(section, i)}{_provenance_note(i)}" for i in value]
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
    newest first, then key values. What doesn't fit is counted with a pointer to the file.
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
              ("Decisions", [_decision_line(d) for d in reversed(ctx.get("decisions") or [])
                             if not d.get("superseded_by")]),
              ("Key values", [i["text"] for i in ctx.get("facts") or []])]
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

_ITEM_KINDS = ["decision", "rule", "value", "fact"]


def main(argv: List[str]) -> int:
    import argparse

    from cognicore import project_suggest

    p = argparse.ArgumentParser(prog="cognicore project",
                                description=f"Curated project context in {FILE.as_posix()}, read first by every new brief.")
    sub = p.add_subparsers(dest="action")
    s = sub.add_parser("set", help="Set the goal or the status")
    s.add_argument("kind", choices=["goal", "status"])
    s.add_argument("text", nargs="+")
    a = sub.add_parser("add", help="Add a decision, rule or key value")
    a.add_argument("kind", choices=_ITEM_KINDS)
    a.add_argument("text", nargs="+")
    a.add_argument("--why", default="", help="the reason, for a decision")
    a.add_argument("--rejected", action="store_true", help="a rejected approach, for a decision")
    r = sub.add_parser("remove", help="Remove an item by its id (D2, R1, V3), or the goal or status")
    r.add_argument("ref", help="an id from `list` (D2), or goal, status, or a kind followed by its number")
    r.add_argument("number", nargs="?", type=int)
    sub.add_parser("list", help="Show the project context")
    h = sub.add_parser("history", help="Every change to the file (its git log)")
    h.add_argument("--diff", action="store_true", help="show what each change did")
    sub.add_parser("path", help="Print where the file is")
    sub.add_parser("suggestions", help="Items suggested from sessions, waiting for you to accept")
    acc = sub.add_parser("accept", help="Add suggested items to the project context")
    acc.add_argument("numbers", nargs="+", type=int)
    dis = sub.add_parser("dismiss", help="Drop suggested items; they aren't suggested again")
    dis.add_argument("numbers", nargs="+", type=int)
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
        ctx, redacted, added = add(args.kind, " ".join(args.text), why=args.why, rejected=args.rejected)
        label = "Key value" if KINDS[args.kind] == "facts" else args.kind.capitalize()
        print(f"{label} {'added to' if added else 'already in'} {path()}")
        note(redacted)
    elif action == "remove":
        ref = args.ref
        if not _REF.match(ref) and ref not in KINDS:
            p.error(f"unknown item {ref!r}: use an id from `cognicore project list`, such as D2")
        if not _REF.match(ref) and KINDS[ref] not in TEXTS and args.number is None:
            p.error(f"say which {ref}: its id in `cognicore project list`, such as D2")
        gone = remove(ref, args.number)
        if gone is None:
            print(f"Nothing to remove: no {ref}" + (f" number {args.number}." if args.number else "."))
            return 1
        print(f"Removed: {gone}")
    elif action == "history":
        print(history(diff=args.diff))
    elif action == "path":
        print(path())
    elif action == "suggestions":
        print(project_suggest.format_pending(project_suggest.pending()))
    elif action == "accept":
        done, missing = project_suggest.accept(args.numbers)
        for line in done:
            print(f"Accepted: {line}")
        if missing:
            print(f"No suggestion numbered {', '.join(map(str, missing))}: see `cognicore project suggestions`.")
        if done:
            print(f"Saved to {path()}")
        return 0 if done else 1
    elif action == "dismiss":
        gone, missing = project_suggest.dismiss(args.numbers)
        for line in gone:
            print(f"Dismissed: {line}")
        if missing:
            print(f"No suggestion numbered {', '.join(map(str, missing))}.")
        return 0 if gone else 1
    else:
        print(format_list(load(), path()))
    return 0

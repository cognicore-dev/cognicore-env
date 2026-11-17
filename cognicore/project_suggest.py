"""
Suggested project context: proposed from sessions, added only when a person accepts.

The Stop hook reads what the session said and proposes at most three items:
a decision the session gave a reason for, a rule-like "always / never / don't"
sentence the user wrote, and a goal the user stated that differs from the
curated one. Agents can propose items too (the ``cognicore_project_propose``
MCP tool). Nothing reaches ``.cognicore/context.md`` until a person runs
``cognicore project accept <n...>``.

Pending suggestions are this user's own, kept in
``~/.cognicore/mcp/project_suggestions.json`` per project. A dismissed or
accepted item is remembered (as a hash) so it is not suggested again.

Environment:
    COGNICORE_SUGGEST=0  turn the Stop-hook suggestions off
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cognicore import project_context as pc

STORE = "project_suggestions.json"
MAX_SHOWN = 3
MAX_PENDING = 20
KINDS = ("decision", "rule", "goal", "status", "value")
_RULE_START = re.compile(r"^(?:please\s+|we\s+|you\s+(?:must|should)\s+)?(always|never|don'?t|do\s+not)\b\s+(?P<rest>.+)",
                         re.I)
# "Don't worry", "never mind": conversation, not a rule.
_NOT_A_RULE = re.compile(r"^(?:please\s+)?(?:never\s+mind|don'?t\s+(?:worry|bother|forget|know|think|mind|"
                         r"understand|see|get|care|have|need|want|like|remember)|do\s+not\s+(?:worry|know|"
                         r"think|have|need|want))\b", re.I)


def _data_dir() -> Path:
    raw = os.environ.get("COGNICORE_DATA_DIR", "")
    folder = Path(raw) if raw else Path.home() / ".cognicore" / "mcp"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _key(cwd: Optional[str] = None) -> str:
    return os.path.normcase(str(pc.root(cwd)))


def _load() -> Dict[str, Any]:
    try:
        data = json.loads((_data_dir() / STORE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: Dict[str, Any]) -> None:
    target = _data_dir() / STORE
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)


def _project(data: Dict[str, Any], cwd: Optional[str]) -> Dict[str, Any]:
    entry = data.setdefault(_key(cwd), {})
    if not isinstance(entry, dict):
        entry = data[_key(cwd)] = {}
    if not isinstance(entry.get("pending"), list):
        entry["pending"] = []
    if not isinstance(entry.get("seen"), list):
        entry["seen"] = []
    entry["pending"] = [s for s in entry["pending"] if isinstance(s, dict) and isinstance(s.get("n"), int)
                        and s.get("kind") in KINDS and isinstance(s.get("text"), str)]
    return entry


def _fingerprint(kind: str, text: str) -> str:
    words = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
    return hashlib.sha256(f"{kind}:{words}".encode("utf-8")).hexdigest()[:16]


def _in_context(kind: str, text: str, ctx: Dict[str, Any]) -> bool:
    if kind in ("goal", "status"):
        return pc.similar(text, ctx.get(kind) or "") >= 0.8
    section = pc.KINDS[kind]
    field = "what" if section == "decisions" else "text"
    return any(pc.similar(text, item.get(field, "")) >= 0.8 for item in ctx.get(section) or [])


def propose(kind: str, text: str, why: str = "", cwd: Optional[str] = None,
            by: str = "", session: str = "") -> Dict[str, Any]:
    """Queue one suggestion. Returns it with ``n`` (its number for ``accept``), or a ``skipped`` reason."""
    kind = (kind or "").strip().lower()
    if kind == "fact":
        kind = "value"
    if kind not in KINDS:
        return {"skipped": f"unknown kind {kind!r}; use one of {', '.join(KINDS)}"}
    text, redacted = pc._clean(text)
    why, more = pc._clean(why)
    if len(text.split()) < 2:
        return {"skipped": "too short to be a project item"}
    if pc._REJECTED.match(text) and kind == "decision":
        text = pc._REJECTED.sub("", text)
    ctx = pc.load(cwd)
    if _in_context(kind, text, ctx):
        return {"skipped": "already in the project context"}
    data = _load()
    entry = _project(data, cwd)
    fp = _fingerprint(kind, text)
    if fp in entry["seen"]:
        return {"skipped": "suggested before"}
    for s in entry["pending"]:
        if s["kind"] == kind and pc.similar(s["text"], text) >= 0.8:
            return {"skipped": "already suggested", "n": s["n"]}
    item = {"n": max((s["n"] for s in entry["pending"]), default=0) + 1, "kind": kind, "text": text,
            "why": why, "date": time.strftime("%Y-%m-%d"), "by": pc._clean(by)[0] if by else "",
            "session": session, "created": time.time()}
    entry["pending"] = (entry["pending"] + [item])[-MAX_PENDING:]
    entry["seen"] = (entry["seen"] + [fp])[-500:]
    _save(data)
    return {**item, "redacted": redacted + more}


def pending(cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    return list(_project(_load(), cwd)["pending"])


def describe(s: Dict[str, Any]) -> str:
    label = {"value": "Key value"}.get(s["kind"], s["kind"].capitalize())
    why = f" (why: {s['why']})" if s.get("why") and s["kind"] == "decision" else ""
    return f"{label}: {s['text']}{why}"


def _short(text: str, limit: int = 110) -> str:
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def format_pending(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "No suggestions waiting. Sessions and agents propose them; you accept them."
    lines = ["Suggested for the project context (accept with `cognicore project accept <n...>`):"]
    for s in items:
        src = ", ".join(p for p in (s.get("date"), f"by {s['by']}" if s.get("by") else "") if p)
        lines.append(f"  {s['n']}. {describe(s)}" + (f"  ({src})" if src else ""))
    return "\n".join(lines)


def _take(numbers: Iterable[int], cwd: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any],
                                                                 List[Dict[str, Any]], List[int]]:
    data = _load()
    entry = _project(data, cwd)
    wanted = list(dict.fromkeys(int(n) for n in numbers))
    found = [s for n in wanted for s in entry["pending"] if s["n"] == n]
    missing = [n for n in wanted if not any(s["n"] == n for s in found)]
    entry["pending"] = [s for s in entry["pending"] if s not in found]
    return data, entry, found, missing


def accept(numbers: Iterable[int], cwd: Optional[str] = None) -> Tuple[List[str], List[int]]:
    """Write the numbered suggestions to the project context. Returns (accepted lines, unknown numbers)."""
    data, _, found, missing = _take(numbers, cwd)
    done = []
    for s in found:
        provenance = {"date": s.get("date") or time.strftime("%Y-%m-%d"), "by": s.get("by") or pc.person()}
        if s.get("session"):
            provenance["session"] = s["session"]
        if s["kind"] in ("goal", "status"):
            pc.set_text(s["kind"], s["text"], cwd)
        else:
            pc.add(s["kind"], s["text"], why=s.get("why", ""), cwd=cwd, provenance=provenance)
        done.append(describe(s))
    if found:
        _save(data)
    return done, missing


def dismiss(numbers: Iterable[int], cwd: Optional[str] = None) -> Tuple[List[str], List[int]]:
    data, _, found, missing = _take(numbers, cwd)
    if found:
        _save(data)
    return [describe(s) for s in found], missing


# --- what a session suggests -----------------------------------------------------------

def _sentences(text: str) -> List[str]:
    text = re.sub(r"```[\s\S]*?```", " ", text or "")
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


_TO_AGENT = re.compile(r"\b(?:you|your|yourself)\b", re.I)
_AGENT_PROMPT = re.compile(r"^(?:you are|act as|you're)\b", re.I)


def rule_sentences(conversation: List[Dict[str, Any]]) -> List[str]:
    """Rule-like sentences the user wrote: "Always ...", "Never ...", "Don't ...", "We never ...". """
    found = []
    for msg in conversation or []:
        if not isinstance(msg, dict) or msg.get("role") != "user" or not isinstance(msg.get("content"), str):
            continue
        for sent in _sentences(msg["content"]):
            sent = sent.strip(" -*>").rstrip()
            if sent.endswith("?") or _NOT_A_RULE.match(sent) or not _RULE_START.match(sent):
                continue
            if _TO_AGENT.search(sent) or _AGENT_PROMPT.match(msg["content"].lstrip()):
                continue  # how the agent should work this time ("never guess from your own memory"), not a project rule
            if not 4 <= len(sent.split()) <= 30:
                continue
            found.append(sent.rstrip(".!") + ".")
    return found


_DECIDED = re.compile(r"^(?:so\s+|ok(?:ay)?,?\s+)?(?:we|i)(?:'ve| have)?\s+(?:decided|agreed|chose|chosen)\s+"
                      r"(?:to\s+|that\s+|on\s+)?", re.I)


_NARRATION = re.compile(r"\b(?:I'll|I will|I'm going to|I am going to|let me|let's|I'd|I need to|I can)\b",
                        re.I)


def _plain(text: Any, capital: bool = True) -> str:
    """A decision as a context item: "We decided to use Decimal." -> "Use Decimal"."""
    text = _DECIDED.sub("", " ".join(str(text or "").split())).strip().rstrip(".;:,")
    return text[:1].upper() + text[1:] if capital else text


def candidates(handoff: Dict[str, Any], conversation: List[Dict[str, Any]],
               ctx: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """(kind, text, why) the session suggests, best first: decisions with a stated reason, user rules, goal."""
    from cognicore.memory.handoff import stated_goal

    out: List[Tuple[str, str, str]] = []
    for d in (handoff or {}).get("decisions") or []:
        if not isinstance(d, dict) or d.get("status") == "rejected":
            continue
        what, why = _plain(d.get("what")), _plain(d.get("why"), capital=False)
        if _NARRATION.search(f"{what} {why}"):
            continue  # the agent describing its next step, not a project decision
        if what and why and why.lower() not in ("none", "n/a") and 2 <= len(what.split()) <= 40:
            out.append(("decision", what, why))
    out += [("rule", r, "") for r in rule_sentences(conversation)]
    goal = stated_goal([m for m in conversation or [] if isinstance(m, dict) and m.get("role") == "user"])
    if goal and 4 <= len(goal.split()) <= 30:
        out.append(("goal", goal, ""))
    return [c for c in out if not _in_context(c[0], c[1], ctx)]


def from_session(handoff: Dict[str, Any], conversation: List[Dict[str, Any]], cwd: str,
                 agent: str = "", session: str = "") -> str:
    """Propose what the session suggests; return the one-line Stop-hook message, or "" if nothing is new."""
    if os.environ.get("COGNICORE_SUGGEST") == "0" or not cwd:
        return ""
    ctx = pc.load(cwd)
    new = []
    for kind, text, why in candidates(handoff, conversation, ctx):
        res = propose(kind, text, why, cwd=cwd, by=agent, session=session)
        if "n" in res and not res.get("skipped"):
            new.append(res)
        if len(new) >= MAX_SHOWN:
            break
    if not new:
        return ""
    shown = "; ".join(f"{s['n']}. {_short(describe(s))}" for s in new)
    numbers = " ".join(str(s["n"]) for s in new)
    return f"CogniCore: {len(new)} suggested for project context: {shown}. Accept with `cognicore project accept {numbers}`."

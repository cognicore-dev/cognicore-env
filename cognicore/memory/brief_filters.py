"""Resume-time filters that keep a brief honest about dates, agents and what is finished.

Saved sessions are kept as they were written, including those saved before an
extraction rule was tightened. These filters apply the current rules when a
brief is built, so old sessions brief as cleanly as new ones:

* date questions ("what did we do yesterday?", "on Nov 15") pick that day's sessions;
* questions that name an agent ("what did Codex do?") pick that agent's sessions;
* open tasks that the same or a newer session reports as done are left out;
* a status line that the session's own "Done" list contradicts is left out;
* noise saved as decisions or facts (headings, quote pieces, narration) is left out.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from cognicore.memory.handoff import _DECISION, _NARRATION, _QUOTED, is_fragment, session_dates

# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_MONTH_DAY = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}(?:,?\s+(\d{{4}}))?", re.I)
_DAYS_AGO = re.compile(r"\b(\d{1,2})\s+days?\s+ago\b", re.I)


def date_in_query(query: Optional[str], today: Optional[date] = None) -> Optional[date]:
    """The calendar day a question asks about, or None.

    Understands "today", "yesterday", "3 days ago", "2026-11-15", "Nov 15",
    "November 15th, 2026" and "15 Nov". A day without a year is the most recent
    such day that is not in the future.
    """
    if not query:
        return None
    today = today or datetime.now().date()
    q = query.lower()
    if re.search(r"\btoday\b", q):
        return today
    if re.search(r"\byesterday\b", q):
        return today - timedelta(days=1)
    m = _DAYS_AGO.search(q)
    if m:
        return today - timedelta(days=int(m.group(1)))
    m = _ISO.search(q)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    for rx, month_group, day_group in ((_MONTH_DAY, 1, 2), (_DAY_MONTH, 2, 1)):
        m = rx.search(q)
        if not m:
            continue
        month, day = _MONTHS[m.group(month_group)[:3].lower()], int(m.group(day_group))
        try:
            if m.group(3):
                return date(int(m.group(3)), month, day)
            found = date(today.year, month, day)
        except ValueError:
            return None
        return found if found <= today else found.replace(year=today.year - 1)
    return None


def session_days(doc: Dict[str, Any]) -> Tuple[str, str]:
    """(first day, last day) the session was worked on, as YYYY-MM-DD."""
    return session_dates(doc.get("provenance") or {})


def on_day(doc: Dict[str, Any], day: date) -> bool:
    """The session was started, worked on or last saved on *day*."""
    first, last = session_days(doc)
    d = day.isoformat()
    return bool(first) and first <= d <= (last or first)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

# How a question names an agent -> the normalized names sessions are saved under.
_AGENTS = {
    "chatgpt": ("chatgpt", "openai"),
    "claude code": ("claudecode",),
    "claude-code": ("claudecode",),
    "claude.ai": ("claudeai", "claude"),
    "claude ai": ("claudeai", "claude"),
    "codex": ("codex",),
    "cursor": ("cursor",),
    "gemini": ("gemini",),
}
_AGENT_RX = re.compile(r"\b(chatgpt|claude[ -]code|claude\.ai|claude ai|codex|cursor|gemini)\b", re.I)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def agent_in_query(query: Optional[str]) -> str:
    """The agent a question names ("ChatGPT", "Claude Code", ...), lowercased, or ""."""
    m = _AGENT_RX.search(query or "")
    return m.group(1).lower().replace("-", " ") if m else ""


def by_agent(doc: Dict[str, Any], agent: str) -> bool:
    """The session was saved by *agent* (as named by :func:`agent_in_query`)."""
    prov = doc.get("provenance") or {}
    names = {_norm(prov.get("agent", "")), _norm(prov.get("client", ""))} - {""}
    wanted = _AGENTS.get(agent, (_norm(agent),))
    return any(n == w or n.startswith(w) for n in names for w in wanted if w)


# ---------------------------------------------------------------------------
# Finished tasks and contradicted status
# ---------------------------------------------------------------------------

_STOP = {"must", "should", "need", "needs", "the", "and", "for", "with", "that", "this", "from", "into", "only",
         "note", "also", "have", "has", "been", "will", "each", "every", "both", "parts", "part", "still"}


def _stems(text: str) -> set:
    out = set()
    for w in re.findall(r"[a-z0-9][a-z0-9-]*", (text or "").lower()):
        if len(w) < 4 or w in _STOP:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                w = w[: -len(suffix)]
                break
        out.add(w)
    return out


def done_by(task: str, completed: List[str]) -> bool:
    """A "Done" item reports this task's work: most of the task's words are in one item.

    "Full suite must pass (note the 3 known failures)" is done by "the full suite
    gives 1470 passed, 7 skipped, and only the 3 known failures".
    """
    words = _stems(_QUOTED.sub(" ", task))
    if len(words) < 2:
        return False
    for item in completed:
        if len(words & _stems(item)) / len(words) >= 0.6:
            return True
    return False


_DENIES = re.compile(r"^(?:nothing|none of (?:it|them|this))\s+(?:is|was|has been|have been)\s+(?:yet\s+)?"
                     r"(merged|deployed|released|pushed|published)\b", re.I)


def consistent_state(state: str, completed: List[str]) -> str:
    """The status line without sentences that the session's own "Done" list contradicts.

    "Nothing is merged." goes when "PR #227 ... is already merged" is listed as done.
    """
    if not state:
        return state
    done = " ".join(completed or []).lower()
    kept = []
    for sent in re.split(r"(?<=[.!?])\s+", state):
        m = _DENIES.match(sent.strip())
        if m and m.group(1).lower() in done:
            continue
        kept.append(sent)
    return " ".join(kept).strip()


# ---------------------------------------------------------------------------
# Noise saved by older extraction rules
# ---------------------------------------------------------------------------

_PROMPT_SPEC = re.compile(r"^(?:part|step|phase)\s+\d+\b|^context:\s|^storage:\s", re.I)


def real_decision(d: Dict[str, Any]) -> bool:
    """A decision that was made, not a heading, a spec line, a quote piece or the agent's narration."""
    what, why = str(d.get("what") or ""), str(d.get("why") or "")
    if not what or is_fragment(what) or _PROMPT_SPEC.match(what) or _NARRATION.match(why) \
            or _NARRATION.match(what) or why.strip().lower() in ("none", "null", "n/a"):
        return False
    # Only the noun "decision" made it one: "Every decision records its source", "add decision ...".
    unquoted = _QUOTED.sub(" ", what)
    if re.match(r"^decision\s*:", unquoted, re.I) or d.get("status") == "rejected":
        return True
    return not (re.search(r"\bdecisions?\b", unquoted, re.I) and not _DECISION.search(
        re.sub(r"\bdecisions?\b", " ", unquoted, flags=re.I)))


def real_fact(fact: str) -> bool:
    """A statement that stands on its own (headings, quote pieces and dangling pronouns don't)."""
    # A fragment's exact values are kept in the session's exact values, so the fragment itself can go.
    return bool(fact) and not is_fragment(fact)


_BRIEF_CODE = re.compile(r"^\s*(?:rules:|=== cognicore|goal:|decisions:)", re.I)


def real_code(c: Dict[str, Any]) -> bool:
    """Code the work produced, not a CogniCore brief or context block that was printed."""
    return not _BRIEF_CODE.match(str(c.get("code") or ""))


def iso_day(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""

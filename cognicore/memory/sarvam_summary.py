"""
AI summaries of synced sessions on the hosted server, with Sarvam AI.

The hosted server never sees transcripts, only each session's handoff, which
rule-based capture makes long and noisy for long sessions. When the server has
``SARVAM_API_KEY`` in its environment, it asks Sarvam to rewrite the summary
part of a synced handoff (goal, state, decisions, open tasks, key facts) into
a shorter one. Exact values, files, code and the "Done" list are kept as
captured. The key never leaves the server: clients only ever receive the
resulting handoff.

Safeguards are the same as :mod:`cognicore.memory.ai_compress`: secrets are
redacted before anything is sent, every item must cite only exact values that
occur in the source handoff (invented ones are dropped), and any error returns
``None`` so the rule-based handoff stays as it was.

Environment (server only):
    SARVAM_API_KEY              turns summaries on; unset = off
    COGNICORE_SARVAM_MODEL      default sarvam-105b
    COGNICORE_SUMMARY_MIN_TOKENS  summarise handoffs at least this long, and
                                again only after this much growth (default 3000)
"""

import copy
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from cognicore.memory.ai_compress import SYSTEM_PROMPT, _keep_grounded
from cognicore.memory.redact import redact_secrets

SARVAM_URL = "https://api.sarvam.ai/v1/chat/completions"
DEFAULT_MODEL = "sarvam-105b"
# sarvam-105b reasons at length before answering, and the reasoning counts against
# max_tokens and grows with the input: a 5k-token session used 15,865 of 16,000.
# The call runs in the background, so it may take minutes; what is sent is capped
# so it stays well inside these limits.
MAX_TOKENS = 32_000
TIMEOUT = 300.0
MAX_SEND_TOKENS = 6_000
# (section, keep newest from the end?) Decisions, facts and Done are in the order they
# were said; open tasks are newest first.
_LISTS = (("completed", True), ("decisions", True), ("open_tasks", False), ("key_facts", True), ("files", False))

JSON_INSTRUCTIONS = """

The input is an automatically extracted, noisy handoff, not a transcript. Many of its "open tasks" and "decisions" are just sentences that contained a keyword: keep only real ones. Merge duplicates. Drop anything already finished.

A decision's "why" is the reason the handoff gives for it, in its words. If the handoff gives no reason, "why" is "": never infer or invent one.
A decision marked "Rejected:" in the handoff stays rejected: its "status" is "rejected", otherwise "made".

Answer with one JSON object and nothing else:
{"goal": "...", "current_state": "...", "decisions": [{"what": "...", "why": "...", "status": "made"}], "open_tasks": ["..."], "key_facts": ["..."], "files": ["..."]}
At most 8 decisions, 10 open tasks and 12 key facts."""


_EMPTY_WHY = {"", "none", "null", "n/a", "na", "-", "unknown", "not stated", "no reason given"}
_WORD = re.compile(r"[a-z0-9][a-z0-9_.\-/]*")


def _why(value: Any) -> str:
    """A decision's reason as text; None and placeholders such as "None" or "n/a" become ""."""
    text = str(value or "").strip()
    return "" if text.lower().strip(" .()") in _EMPTY_WHY else text


def _reason_grounded(why: str, source_lower: str) -> bool:
    """Whether *why* is a reason the source gives: most of its content words occur there.

    The model tends to fill an empty "why" with a plausible reason ("reduced for
    efficiency"). Value grounding can't catch that, since it contains no value.
    """
    words = [w for w in _WORD.findall(why.lower()) if len(w) > 3]
    if not words:
        return True
    found = sum(1 for w in words if w in source_lower)
    return found / len(words) >= 0.75


def _ground_reasons(summary: Dict[str, Any], source: str) -> int:
    """Empty every decision's "why" that the source doesn't state; returns how many."""
    source_lower, dropped = source.lower(), 0
    for d in summary.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        why = _why(d.get("why"))
        if why and not _reason_grounded(why, source_lower):
            why, dropped = "", dropped + 1
        d["why"] = why
    return dropped


def _rejected_in(doc: Dict[str, Any]) -> List[str]:
    return [str(d.get("what", "")).strip().lower() for d in doc.get("decisions") or []
            if isinstance(d, dict) and d.get("status") == "rejected" and d.get("what")]


def _is_rejected(d: Dict[str, Any], what: str, rejected_source: List[str]) -> Tuple[bool, str]:
    """Whether a summary decision is a rejected one, and its text without a "Rejected:" prefix."""
    m = re.match(r"\s*rejected\s*[:\-–—]\s*", what, re.I)
    if m:
        return True, what[m.end():].strip()
    if str(d.get("status", "")).strip().lower() == "rejected":
        return True, what
    low = what.lower()
    # The model sometimes drops the prefix: a decision that repeats a rejected one stays rejected.
    return any(low and (low in r or r in low) for r in rejected_source), what


def min_tokens() -> int:
    try:
        return int(os.environ.get("COGNICORE_SUMMARY_MIN_TOKENS", "3000"))
    except ValueError:
        return 3000


def sendable(doc: Dict[str, Any], max_tokens: int = MAX_SEND_TOKENS) -> Dict[str, Any]:
    """*doc* with its lists cut to the newest items, so its source text fits *max_tokens*."""
    if len(source_text(doc)) // 4 <= max_tokens:
        return doc
    cap = max(len(doc.get(k) or []) for k, _ in _LISTS)
    out = dict(doc)
    while cap > 1 and len(source_text(out)) // 4 > max_tokens:
        cap //= 2
        for key, newest_last in _LISTS:
            items = list(doc.get(key) or [])
            out[key] = items[-cap:] if newest_last else items[:cap]
    return out


def source_text(doc: Dict[str, Any]) -> str:
    """The summary part of a handoff as plain text: what is sent, and what grounding checks against."""
    lines = []
    for key, label in (("goal", "Goal"), ("current_state", "State")):
        if doc.get(key):
            lines.append(f"{label}: {doc[key]}")
    for title, items in (
        ("Done", doc.get("completed") or []),
        ("Decisions", [f"{'Rejected: ' if d.get('status') == 'rejected' else ''}{d.get('what', '')}"
                       + (f" (why: {d['why']})" if d.get("why") else "") for d in doc.get("decisions") or []]),
        ("Open tasks", [t.get("task", "") for t in doc.get("open_tasks") or [] if t.get("status") == "open"]),
        ("Key facts", doc.get("key_facts") or []),
        ("Files", doc.get("files") or []),
    ):
        items = [str(i) for i in items if i]
        if items:
            lines.append(f"{title}:")
            lines.extend(f"- {i}" for i in items)
    return "\n".join(lines)


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    text = re.sub(r"<think>[\s\S]*?</think>", " ", text or "")
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    try:
        value = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _post(url: str, body: Dict[str, Any], api_key: str, timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "api-subscription-key": api_key,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - fixed https endpoint
        return json.loads(resp.read() or b"{}")


def summarize_handoff(
    doc: Dict[str, Any],
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    post: Callable[[str, Dict[str, Any], str, float], Dict[str, Any]] = _post,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Summarise a handoff with Sarvam. Returns ``(summary, info)``; summary is None when skipped."""
    api_key = api_key or os.environ.get("SARVAM_API_KEY", "")
    model = model or os.environ.get("COGNICORE_SARVAM_MODEL") or DEFAULT_MODEL
    sent = sendable(doc)
    text, redactions = redact_secrets(source_text(sent))
    info: Dict[str, Any] = {"method": "ai", "provider": "sarvam", "model": model,
                            "redactions": redactions, "source_tokens": len(text) // 4}
    if sent is not doc:
        info["trimmed_from_tokens"] = len(source_text(doc)) // 4
    if not api_key:
        return None, {**info, "skipped": "no SARVAM_API_KEY"}
    if not text.strip():
        return None, {**info, "skipped": "empty"}
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT + JSON_INSTRUCTIONS},
                     {"role": "user", "content": f"<handoff>\n{text}\n</handoff>"}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.2,
    }
    try:
        res = post(SARVAM_URL, body, api_key, TIMEOUT)
    except urllib.error.HTTPError as e:
        return None, {**info, "skipped": f"API error {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, {**info, "skipped": f"network error: {type(e).__name__}"}
    choice = (res.get("choices") or [{}])[0]
    usage = res.get("usage") or {}
    info.update(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0))
    if choice.get("finish_reason") == "length":
        return None, {**info, "skipped": "summary cut off at max_tokens"}
    summary = _parse_json((choice.get("message") or {}).get("content") or "")
    if summary is None:
        return None, {**info, "skipped": "invalid JSON"}
    summary, dropped = _keep_grounded(summary, text)
    info["dropped_ungrounded"] = dropped
    info["dropped_reasons"] = _ground_reasons(summary, text)
    if not (summary.get("goal") or summary.get("current_state") or summary.get("open_tasks")):
        return None, {**info, "skipped": "empty summary"}
    return summary, info


def apply_summary(doc: Dict[str, Any], summary: Dict[str, Any], info: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of *doc* whose summary part is replaced; values, files, code and Done are kept.

    Items too old to be sent (see :func:`sendable`) are kept after the summary's
    own, as captured, so cutting the input never loses anything.
    """
    out = copy.deepcopy(doc)
    sent = sendable(doc)
    unsent = {key: [i for i in doc.get(key) or [] if i not in (sent.get(key) or [])]
              for key in ("decisions", "open_tasks", "key_facts")}
    for key in ("goal", "current_state"):
        if summary.get(key):
            out[key] = summary[key]
    decisions: List[Dict[str, str]] = []
    rejected_source = _rejected_in(doc)
    for d in summary.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        rejected, what = _is_rejected(d, str(d.get("what") or "").strip(), rejected_source)
        if what:
            decisions.append({"what": what, "why": _why(d.get("why")), "status": "rejected" if rejected else "made"})
    out["decisions"] = decisions
    out["open_tasks"] = [{"task": t, "status": "open"} for t in summary.get("open_tasks") or []]
    seen = {f.lower() for f in out.get("files") or []}
    out["files"] = list(out.get("files") or []) + [f for f in summary.get("files") or [] if f.lower() not in seen]
    # The model often repeats a file, a ticket or a decision as a "fact": keep only new information.
    elsewhere = " | ".join([out.get("goal", ""), out.get("current_state", "")] + out["files"]
                            + [f"{d['what']} {d['why']}" for d in decisions]
                            + [t["task"] for t in out["open_tasks"]]).lower()
    out["key_facts"] = [f for f in summary.get("key_facts") or [] if f.strip().lower() not in elsewhere]
    out["decisions"] = unsent["decisions"] + out["decisions"]  # older ones first, as captured
    out["open_tasks"] += unsent["open_tasks"]  # newest first, so the unsent (older) go last
    out["key_facts"] = unsent["key_facts"] + out["key_facts"]
    out["compression"] = {k: v for k, v in info.items() if k != "skipped"}
    return out

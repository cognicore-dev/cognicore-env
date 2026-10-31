"""
Versioned session handoff — the unit of memory transfer between agents.

A handoff is a small JSON document (schema ``cognicore.handoff/v1``, see
``docs/handoff-schema.json``) built from a conversation at save time, so an
agent in another client can resume the work without re-explaining:

    goal, current_state, decisions (what + why), open_tasks, key_facts,
    exact_values, files, code, environment, provenance

Extraction is rule based and makes zero LLM calls. Exact values (ids,
versions, hashes, paths, numbers, error codes, env var names) are copied
verbatim, chatter is dropped and repeated statements are merged.

Resume uses :func:`select_for_budget` to return the most important items
first within a token budget and to report what was left out, so the rest
can be fetched on demand.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from cognicore.memory.redact import redact_secrets

SCHEMA_VERSION = "cognicore.handoff/v1"

# Resume order: most important first.
SECTION_PRIORITY = [
    "goal", "current_state", "environment_warnings", "open_tasks", "decisions",
    "key_facts", "files", "exact_values", "experiences", "code", "other_sessions",
]
SECTIONS = SECTION_PRIORITY + ["provenance", "environment"]

ENV_KEYS = ("python_version", "os", "framework", "framework_version", "dependencies")


# ---------------------------------------------------------------------------
# Sentence helpers (shared with context_preservation)
# ---------------------------------------------------------------------------

def extract_sentences(text: str) -> List[str]:
    """Split text into sentences cleanly, protecting decimal numbers and file extensions."""
    if not text:
        return []
    # Protect decimal numbers (e.g. 98.2, 98. 2) so dots aren't treated as sentence boundaries
    protected_text = re.sub(r'(\d+)\.\s*(\d+)', r'\1__DOT__\2', text)

    # Split on periods, exclamation marks, question marks followed by space/newline, or newlines
    raw_sentences = re.split(r'(?<=[.!?])\s+|\n+', protected_text)

    # Restore decimal points
    restored = [s.replace("__DOT__", ".").strip() for s in raw_sentences]
    return [s for s in restored if s and len(s) > 5]


def normalize_sentence(s: str) -> str:
    """Normalize sentence for deduplication."""
    return re.sub(r'[^a-z0-9]', '', s.lower())


def clean_statement(s: str) -> str:
    """Clean bullet markers and whitespace from sentence while keeping full content."""
    cleaned = re.sub(r'^(?:[-*•]|\d+\.)\s*', '', s.strip())
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Exact values
# ---------------------------------------------------------------------------

_MONTHS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
           r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_FILE_EXT = (r"(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|sql|prisma|json|ya?ml|toml|ini|cfg|md|txt|csv|"
             r"go|rs|java|kt|rb|php|cs|cpp|c|h|hpp|swift|sh|ps1|html|css|scss|lock|env|ipynb|proto)")

# (kind, pattern) — earlier patterns claim their span first.
_VALUE_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("redacted", re.compile(r"\[REDACTED:[a-z_]+\]")),
    ("url", re.compile(r"\bhttps?://[^\s<>\"')\]]+[^\s<>\"')\].,;:!?]")),
    ("money", re.compile(r"[$€£¥₹]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|bn|million|billion))?(?:/\w+)?")),
    ("path", re.compile(r"(?<![\w@])(?:\.{0,2}/)?(?:[\w.\-]+/)+[\w.\-]*[\w\-]")),
    ("file", re.compile(rf"\b[\w\-]+(?:\.[\w\-]+)*\.{_FILE_EXT}\b")),
    ("domain", re.compile(
        r"\b(?:[a-z0-9-]+\.)+(?:com|dev|io|ai|org|net|app|co|sh|xyz|cloud|tech|so|me|us|uk|in)\b", re.I)),
    ("inline_code", re.compile(r"`([^`\n]{2,80})`")),
    ("assignment", re.compile(r"\b[\w.\-]+=[^\s,;]+")),
    ("env_var", re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")),
    ("ticket", re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")),
    ("color", re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")),
    ("date", re.compile(rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b|"
                        r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?\b")),
    ("version", re.compile(
        r"(?:\b[A-Za-z][\w+\-]*\s+)?\bv?\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.]+)?\b"
        r"(?!\s*(?:%|percent|million|billion|thousand|k\b|x\b))")),
    ("percent", re.compile(r"\b\d+(?:\.\d+)?\s?(?:%|percent\b)")),
    ("quantity", re.compile(
        r"\b\d[\d,]*(?:\.\d+)?\s?(?:million|billion|thousand|k|ms|seconds?|secs?|minutes?|mins?|"
        r"hours?|h|days?|d|weeks?|months?|years?|GB|MB|KB|TB|rows|users|signups|requests|rps|qps|x)\b")),
    ("number", re.compile(r"\b\d{1,3}(?:,\d{3})+\b")),
    ("hash", re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")),
    ("code", re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{4,}\b")),
    ("identifier", re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")),
    ("http_status", re.compile(r"\b[1-5]\d\d(?=s?\b)(?=\s*(?:s\b|errors?|responses?|status))")),
]


def extract_exact_values(text: str) -> List[Dict[str, str]]:
    """Return ``[{"value", "kind"}]`` for every exact value in *text*, verbatim, in order."""
    taken: List[Tuple[int, int]] = []
    found: List[Tuple[int, str, str]] = []
    for kind, pattern in _VALUE_PATTERNS:
        for m in pattern.finditer(text):
            start, end = m.span()
            if any(start < e and s < end for s, e in taken):
                continue
            taken.append((start, end))
            value = (m.group(1) if kind == "inline_code" else m.group(0)).strip()
            # Redaction markers claim their span but are not values worth keeping.
            if kind != "redacted" and len(value) >= 2:
                found.append((start, kind, value))
    found.sort()
    return [{"value": v, "kind": k} for _, k, v in found]


# ---------------------------------------------------------------------------
# Sentence classification
# ---------------------------------------------------------------------------

_ACKS = {
    "hey", "hi", "hello", "ok", "okay", "sure", "thanks", "thank", "thx", "cheers", "great",
    "nice", "perfect", "cool", "yes", "yeah", "yep", "no", "nope", "noted", "got", "sounds",
    "awesome", "right", "alright", "understood", "agreed", "lgtm", "done", "good",
}

_GOAL = re.compile(
    r"\b(?:(?:our|the|my|main)\s+goal|goal\s+is|objective|we(?:'re| are)\s+(?:trying|migrating|building|"
    r"working)|trying to|working on|let'?s (?:plan|build|fix|migrate)|the task is|aim is|we want to)\b", re.I)
_DECISION = re.compile(
    r"\b(?:decided|decide to|decision|agreed to|we agreed|chose|opted|selected|will use|let'?s use|going with|"
    r"settled on|switch(?:ed)? (?:from|to)|we picked|went with)\b", re.I)
_REJECTED = re.compile(
    r"\b(?:did not help|didn'?t help|did not work|didn'?t work|no luck|ruled out|we rejected|"
    r"rejected (?:the|this|that) (?:idea|approach|option|proposal)|made it worse|"
    r"doesn'?t work|does not work|gave up on)\b", re.I)
_TASK = re.compile(
    r"(?:^|\b)(?:todo|to-do|next step|action item|need(?:s)? to|must|pending|follow[- ]up|remaining|"
    r"still have to|blocked on|we should|don'?t forget)\b|- \[[ x]\]", re.I)
_DONE = re.compile(r"- \[x\]|\b(?:done|completed|finished|shipped|merged)\s*[:\-]", re.I)
_STATE = re.compile(
    r"\b(?:root cause|currently|status|is working|works now|fixed|failed with|failing|blocked|committed|"
    r"deployed|merged|released|pushed|passing|broken|error)\b", re.I)
_STALLING = re.compile(
    r"\b(?:let me (?:think|check|look|see)|get back to you|take your time|wait here|ready to continue|"
    r"one (?:sec|second|moment)|happy to help|let me know|that'?s all|anything else|take a look)\b", re.I)
_WHY = re.compile(r"\s+(?:because|since|so that|so|as)\s+|,\s+(?:because|since|so)\s+", re.I)


def is_chatter(sentence: str) -> bool:
    """Greetings, acknowledgements and short bare questions carry no transferable state."""
    s = sentence.strip()
    words = re.findall(r"[a-z']+", s.lower())
    if not words:
        return True
    if extract_exact_values(s):
        return False
    if s.endswith("?") and len(words) <= 10:
        return True
    if any(p.search(s) for p in (_TASK, _DECISION, _REJECTED, _STATE, _GOAL)):
        return False
    if words[0].strip("'") in _ACKS and len(words) <= 4:
        return True
    return bool(_STALLING.search(s))


def _split_why(sentence: str) -> Tuple[str, str]:
    m = _WHY.search(sentence)
    if not m:
        return sentence, ""
    return sentence[:m.start()].strip(" ,;"), sentence[m.end():].strip()


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def conversation_hash(conversation: List[Any]) -> str:
    """Stable content hash of a conversation, used to make re-saves idempotent."""
    norm = []
    for m in conversation or []:
        if isinstance(m, dict):
            norm.append([str(m.get("role", "")), m.get("content", "")])
        else:
            norm.append(["", str(m)])
    return hashlib.sha256(json.dumps(norm, sort_keys=True, default=str).encode()).hexdigest()


def normalize_environment(env: Any) -> Dict[str, Any]:
    """Accept a dict or JSON string; keep only the EnvironmentContext keys."""
    if not env:
        return {}
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(env, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in ENV_KEYS:
        val = env.get(key)
        if key == "dependencies":
            if isinstance(val, dict) and val:
                out[key] = {str(k): str(v) for k, v in val.items()}
        elif val:
            out[key] = str(val)
    return out


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_handoff(
    conversation: List[Any],
    session_id: str,
    session_name: str,
    created_at: float,
    agent: str = "",
    client: str = "",
    goal: str = "",
    current_state: str = "",
    environment: Any = None,
    migrated: bool = False,
    supplied: Any = None,
) -> Dict[str, Any]:
    """Build a ``cognicore.handoff/v1`` document from a conversation and/or an
    agent-written partial handoff (``supplied``, see :func:`parse_supplied_handoff`).

    Text is redacted before extraction, so the handoff never contains
    detected secrets. Explicit ``goal`` / ``current_state`` win over
    ``supplied`` ones, which win over the extracted ones; supplied decisions,
    tasks, facts and files come first, followed by extracted ones that do not
    repeat them. Raises :class:`HandoffInputError` for an unusable ``supplied``.
    """
    supplied, redactions = parse_supplied_handoff(supplied)
    sentences: List[Tuple[str, str]] = []  # (role, sentence)
    code: List[Dict[str, str]] = []
    seen_code = set()

    for msg in conversation or []:
        if isinstance(msg, dict):
            role, content = str(msg.get("role", "")), msg.get("content", "")
        else:
            role, content = "", str(msg)
        if not isinstance(content, str) or not content.strip():
            continue
        content, n = redact_secrets(content)
        redactions += n

        for lang, body in re.findall(r"```(\w*)\n?([\s\S]*?)```", content):
            body = body.strip()
            if body and body not in seen_code:
                seen_code.add(body)
                before = content.split("```", 1)[0].strip()
                ctx = extract_sentences(before)
                code.append({"lang": lang, "code": body, "context": ctx[-1] if ctx else ""})
        text = re.sub(r"```[\s\S]*?```", " ", content)
        for sent in extract_sentences(text):
            cleaned = clean_statement(sent)
            if cleaned:
                sentences.append((role, cleaned))

    kept = _dedupe([(r, s) for r, s in sentences if not is_chatter(s)])

    found_goal, states, decisions, tasks, facts = "", [], [], [], []
    for role, s in kept:
        if _TASK.search(s):
            tasks.append({"task": s, "status": "done" if _DONE.search(s) else "open"})
        elif _REJECTED.search(s):
            decisions.append({"what": s, "why": "", "status": "rejected"})
        elif _DECISION.search(s):
            what, why = _split_why(s)
            decisions.append({"what": what, "why": why, "status": "made"})
        elif not found_goal and _GOAL.search(s) and not s.endswith("?"):
            found_goal = s
        elif _STATE.search(s):
            states.append(s)
        else:
            facts.append(s)

    if not found_goal:
        # First substantive user statement is the best zero-LLM guess at the goal.
        for role, s in kept:
            if role in ("user", "") and s in facts:
                found_goal = s
                facts.remove(s)
                break

    if supplied:
        decisions = _merge_unique(supplied["decisions"], decisions, lambda d: d["what"])
        tasks = _merge_unique(supplied["open_tasks"], tasks, lambda t: t["task"])
        facts = _merge_unique(supplied["key_facts"], facts, lambda f: f)
        found_goal = supplied.get("goal") or found_goal
        if supplied.get("current_state") and not current_state:
            current_state = supplied["current_state"]

    statements = [s for _, s in kept] + [found_goal, current_state or ""]
    statements += [d["what"] + " " + d["why"] for d in decisions] + [t["task"] for t in tasks] + facts
    statements += (supplied or {}).get("files", [])
    exact: List[Dict[str, str]] = []
    seen_values = set()
    for s in statements:
        for v in extract_exact_values(s or ""):
            if v["value"] not in seen_values:
                seen_values.add(v["value"])
                exact.append(v)
    files = _merge_unique((supplied or {}).get("files", []),
                          [v["value"] for v in exact if v["kind"] in ("path", "file", "url", "domain")],
                          lambda f: f)

    goal_text = redact_secrets(goal)[0].strip() if goal else found_goal
    if current_state:
        state_text, older_states = redact_secrets(current_state)[0].strip(), states
    else:
        # The latest few status statements describe where the work stands now.
        state_text, older_states = " ".join(states[-3:]), states[:-3]
    # Facts with exact values first, then plain statements.
    facts.sort(key=lambda s: 0 if extract_exact_values(s) else 1)
    facts = older_states + facts

    return {
        "schema": SCHEMA_VERSION,
        "provenance": {
            "session_id": session_id,
            "session_name": session_name,
            "agent": agent or "",
            "client": client or "",
            "created_at": _utc_iso(created_at),
            "message_count": len(conversation or []),
            "migrated": migrated,
        },
        "goal": goal_text,
        "current_state": state_text,
        "decisions": decisions,
        "open_tasks": tasks,
        "key_facts": facts,
        "exact_values": exact,
        "files": files,
        "code": code,
        "environment": normalize_environment(environment),
        "redactions": redactions,
    }


class HandoffInputError(ValueError):
    """The agent-supplied handoff is not usable."""


def parse_supplied_handoff(raw: Any) -> Tuple[Dict[str, Any], int]:
    """Normalize an agent-written partial handoff (dict or JSON string).

    Lets the agent send what it already knows (goal, decisions, tasks, facts)
    instead of re-sending the whole conversation, which costs output tokens
    proportional to its length. Strings are redacted like conversation text.
    Returns ``(normalized, redaction_count)``.
    """
    if not raw:
        return {}, 0
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HandoffInputError(f"handoff must be a JSON object: {e.msg}") from e
    if not isinstance(raw, dict):
        raise HandoffInputError("handoff must be a JSON object")

    count = 0

    def clean(value: Any) -> str:
        nonlocal count
        text, n = redact_secrets(str(value).strip())
        count += n
        return text

    def as_list(key: str) -> List[Any]:
        value = raw.get(key) or []
        return value if isinstance(value, list) else [value]

    out: Dict[str, Any] = {}
    for key in ("goal", "current_state"):
        if raw.get(key):
            out[key] = clean(raw[key])
    decisions = []
    for d in as_list("decisions"):
        if isinstance(d, dict):
            what = clean(d.get("what", ""))
            status = d.get("status") if d.get("status") in ("made", "rejected") else "made"
            decisions.append({"what": what, "why": clean(d.get("why", "")) if d.get("why") else "", "status": status})
        elif str(d).strip():
            text = clean(d)
            rejected = text.lower().startswith("rejected:")
            what, why = _split_why(text[len("rejected:"):].strip() if rejected else text)
            decisions.append({"what": what, "why": why, "status": "rejected" if rejected else "made"})
    out["decisions"] = [d for d in decisions if d["what"]]
    tasks = []
    for t in as_list("open_tasks"):
        if isinstance(t, dict):
            status = t.get("status") if t.get("status") in ("open", "done") else "open"
            tasks.append({"task": clean(t.get("task", "")), "status": status})
        elif str(t).strip():
            tasks.append({"task": clean(t), "status": "open"})
    out["open_tasks"] = [t for t in tasks if t["task"]]
    out["key_facts"] = [clean(f) for f in as_list("key_facts") if str(f).strip()]
    out["files"] = [clean(f) for f in as_list("files") if str(f).strip()]
    return out, count


def _merge_unique(first: List[Any], second: List[Any], text_of: Callable[[Any], str]) -> List[Any]:
    """Agent-supplied items first, then extracted ones that do not repeat them."""
    merged, norms = [], []
    for item in first + second:
        n = normalize_sentence(text_of(item))
        if not n or any(n in m or m in n for m in norms):
            continue
        norms.append(n)
        merged.append(item)
    return merged


def _dedupe(items: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Drop repeated statements, including ones fully contained in a longer statement."""
    norms = [normalize_sentence(s) for _, s in items]
    out, seen = [], set()
    for i, (role, s) in enumerate(items):
        n = norms[i]
        if not n or n in seen:
            continue
        if any(n != other and n in other for other in norms):
            continue
        seen.add(n)
        out.append((role, s))
    return out


def validate_handoff(doc: Any) -> List[str]:
    """Return a list of problems; empty means the document is a valid v1 handoff."""
    errors: List[str] = []
    if not isinstance(doc, dict):
        return ["handoff must be an object"]
    if doc.get("schema") != SCHEMA_VERSION:
        errors.append(f"schema must be {SCHEMA_VERSION!r}")
    for key in ("goal", "current_state"):
        if not isinstance(doc.get(key), str):
            errors.append(f"{key} must be a string")
    for key in ("decisions", "open_tasks", "key_facts", "exact_values", "files", "code"):
        if not isinstance(doc.get(key), list):
            errors.append(f"{key} must be a list")
    for d in doc.get("decisions") or []:
        if not isinstance(d, dict) or not isinstance(d.get("what"), str) or d.get("status") not in ("made", "rejected"):
            errors.append("decisions[] items need 'what' and status 'made'|'rejected'")
            break
    for t in doc.get("open_tasks") or []:
        if not isinstance(t, dict) or not isinstance(t.get("task"), str) or t.get("status") not in ("open", "done"):
            errors.append("open_tasks[] items need 'task' and status 'open'|'done'")
            break
    prov = doc.get("provenance")
    if not isinstance(prov, dict) or not prov.get("session_id") or not prov.get("created_at"):
        errors.append("provenance needs session_id and created_at")
    if not isinstance(doc.get("environment"), dict):
        errors.append("environment must be an object")
    return errors


def handoff_from_legacy(rows: List[Dict[str, Any]], session_id: str, session_name: str,
                        created_at: float) -> Dict[str, Any]:
    """Rebuild a v1 handoff from a pre-v1 session's memory rows (read-only upgrade)."""
    conv = []
    for r in rows:
        if r.get("category") == "session_summary":
            continue
        conv.append({"role": "", "content": r.get("text") or ""})
    doc = build_handoff(conv, session_id, session_name or "", created_at, migrated=True)
    doc["provenance"]["message_count"] = None
    return doc


# ---------------------------------------------------------------------------
# Environment check
# ---------------------------------------------------------------------------

def environment_warnings(saved: Dict[str, Any], current: Any) -> List[str]:
    """Compare the saved fingerprint with the resuming agent's environment."""
    current = normalize_environment(current)
    if not saved or not current:
        return []
    try:
        from cognicore.experience.compatibility import EnvironmentChecker
        from cognicore.experience.schema import EnvironmentContext
    except ImportError:  # pragma: no cover - experience package is part of the repo
        return []
    res = EnvironmentChecker().check(EnvironmentContext.from_dict(saved), EnvironmentContext.from_dict(current))
    return [f"BLOCKER: {b}" for b in res.blockers] + list(res.warnings)


# ---------------------------------------------------------------------------
# Budgeted selection and rendering
# ---------------------------------------------------------------------------

def _estimate(text: str) -> int:
    return len(text) // 4


# Lead-ins that only restate the section heading. Stripped when rendering the
# brief (the stored handoff keeps the verbatim sentence); exact values never
# sit inside them.
_LEAD_INS = {
    "goal": re.compile(r"^(?:(?:our|the|my|main)\s+)?(?:goal|objective|aim|task)\s+is\s+(?:to\s+)?", re.I),
    "made": re.compile(
        r"^(?:so\s+)?(?:we|i)\s+(?:have\s+|'ve\s+)?(?:decided|agreed|chose|opted|settled|went)\s+"
        r"(?:to\s+|on\s+|for\s+|with\s+)?", re.I),
    "rejected": re.compile(r"^(?:we|i)\s+(?:tried|attempted)\s+", re.I),
    "task": re.compile(r"^(?:todo|to-do|next step|action item|pending|follow[- ]up)\s*[:\-]\s*", re.I),
}


def _trim(kind: str, text: str) -> str:
    out = _LEAD_INS[kind].sub("", text, count=1).strip()
    return out[:1].upper() + out[1:] if out else text


def _line_for(section: str, item: Any) -> str:
    if section == "goal":
        return f"Goal: {_trim('goal', item)}"
    if section == "current_state":
        return f"State: {item}"
    if section == "environment_warnings":
        return f"! {item}"
    if section == "open_tasks":
        return f"- {_trim('task', item['task'])}"
    if section == "decisions":
        if item.get("status") == "rejected":
            return f"- Rejected: {_trim('rejected', item['what'])}"
        return f"- {_trim('made', item['what'])}" + (f" (why: {item['why']})" if item.get("why") else "")
    if section == "code":
        head = f"{item['context']}\n" if item.get("context") else ""
        return f"{head}```{item.get('lang', '')}\n{item['code']}\n```"
    if section == "exact_values":
        return item["value"]
    if section == "experiences":
        return f"- {item['task']} -> {item['solution']} [{item['status']}]"
    if section == "other_sessions":
        parts = [f"- {item['session_name']} ({item['session_id']}, {item['created_at'][:10]}): "
                 f"{_trim('goal', item['goal'])}"]
        for label, key in (("state", "current_state"), ("open", "open_tasks"),
                           ("decided", "decisions"), ("facts", "key_facts")):
            val = item.get(key)
            if val:
                parts.append(f"  {label}: " + ("; ".join(val) if isinstance(val, list) else val))
        return "\n".join(parts)
    return f"- {item}"


_HEADINGS = {
    "environment_warnings": "Environment warnings:",
    "open_tasks": "Open tasks:",
    "decisions": "Decisions:",
    "key_facts": "Facts:",
    "files": "Files:",
    "exact_values": "Values:",
    "experiences": "Verified experiences:",
    "code": "Code:",
    "other_sessions": "Other sessions:",
}


def select_for_budget(
    sections: Dict[str, Any],
    max_tokens: Optional[int],
    cost: Callable[[str, Any], int],
    fixed_cost: int = 0,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Fill sections in priority order until the budget is used.

    Items that do not fit are skipped (smaller later items may still fit), so
    the most important information is always kept first. Returns the
    included sections and a count of omitted items per section.
    """
    used = fixed_cost
    included: Dict[str, Any] = {}
    omitted: Dict[str, int] = {}
    for section in SECTION_PRIORITY:
        value = sections.get(section)
        if not value:
            continue
        items = value if isinstance(value, list) else [value]
        kept = []
        heading_cost = cost(section, None)
        for item in items:
            c = cost(section, item) + (heading_cost if not kept else 0)
            if max_tokens is None or used + c <= max_tokens:
                kept.append(item)
                used += c
            else:
                omitted[section] = omitted.get(section, 0) + 1
        if kept:
            included[section] = kept if isinstance(value, list) else kept[0]
    return included, omitted


def text_cost(section: str, item: Any) -> int:
    if item is None:
        heading = _HEADINGS.get(section)
        return _estimate(heading) + 1 if heading else 0
    if section in ("files", "exact_values"):
        return _estimate((item["value"] if isinstance(item, dict) else item) + ", ")
    return _estimate(_line_for(section, item)) + 1


def json_cost(section: str, item: Any) -> int:
    if item is None:
        return _estimate(json.dumps(section)) + 2
    return _estimate(json.dumps(item)) + 1


def render_text(included: Dict[str, Any], provenance: Dict[str, Any], more: str = "") -> str:
    """Compact text brief. The session id is only printed in the "more" pointer, where it is needed."""
    head = [f"Session: {provenance.get('session_name') or ''}", (provenance.get("created_at") or "")[:10]]
    if provenance.get("agent"):
        head.append(f"by {provenance['agent']}" + (f" via {provenance['client']}" if provenance.get("client") else ""))
    elif provenance.get("client"):
        head.append(f"via {provenance['client']}")
    if provenance.get("migrated"):
        head.append("migrated from pre-v1 session")
    lines = ["=== COGNICORE CONTEXT RESUME ===", " | ".join(h for h in head if h)]
    for section in SECTION_PRIORITY:
        value = included.get(section)
        if not value:
            continue
        if section in ("goal", "current_state"):
            lines.append(_line_for(section, value))
            continue
        if section in ("files", "exact_values"):
            vals = [v["value"] if isinstance(v, dict) else v for v in value]
            lines.append(f"{_HEADINGS[section]} " + ", ".join(vals))
            continue
        lines.append(_HEADINGS[section])
        lines.extend(_line_for(section, item) for item in value)
    if more:
        lines.append(more)
    return "\n".join(lines).strip()


def handoff_text_for_matching(doc: Dict[str, Any]) -> str:
    """All human-readable text of a handoff, for ranking sessions against a query."""
    parts = [doc.get("goal", ""), doc.get("current_state", "")]
    parts += [d.get("what", "") + " " + d.get("why", "") for d in doc.get("decisions") or []]
    parts += [t.get("task", "") for t in doc.get("open_tasks") or []]
    parts += list(doc.get("key_facts") or [])
    parts += list(doc.get("files") or [])
    parts.append(doc.get("provenance", {}).get("session_name", ""))
    return " ".join(p for p in parts if p).lower()

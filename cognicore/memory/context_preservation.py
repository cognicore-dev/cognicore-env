"""
Context Preservation System for CogniCore Memory.

Provides automatic context compression, session archiving, and session resumption
to solve token exhaustion in long LLM conversations without requiring additional LLM API calls.
"""

import re
import json
import time
import uuid
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

from cognicore.memory.base import MemoryEntry, MemoryScope
from cognicore.memory.decompose import decompose
from cognicore.memory.categorize import auto_categorize
from cognicore.memory.handoff import (
    HandoffInputError,
    SCHEMA_VERSION,
    SECTION_PRIORITY,
    SECTIONS,
    build_handoff,
    clean_statement as _clean_statement,
    conversation_hash,
    environment_warnings,
    extract_sentences as _extract_sentences,
    handoff_from_legacy,
    handoff_text_for_matching,
    json_cost,
    normalize_environment,
    normalize_sentence as _normalize_sentence,
    render_text,
    select_for_budget,
    text_cost,
    validate_handoff,
)
from cognicore.memory.redact import redact_secrets

logger = logging.getLogger("cognicore.context_preservation")

# Keywords for zero-LLM extractive compression
COMPRESSION_KEYWORDS = {
    "decisions", "agreed", "solved", "fixed", "found",
    "learned", "next", "todo", "important", "result",
    "numbers", "percentages", "dates", "names",
    "decision", "agree", "solve", "fix", "find", "learn",
    "percent", "date", "name", "error", "bug", "issue",
    "success", "fail", "test", "pass", "implemented", "created",
    "changed", "updated", "removed", "added", "bugfix", "goal"
}


class TokenBudget:
    """Manages context window token thresholds and automatic preservation triggers."""
    WARNING_THRESHOLD  = 150_000  # warn user
    COMPRESS_THRESHOLD = 170_000  # auto compress
    SAVE_THRESHOLD     = 190_000  # force save session

    @staticmethod
    def estimate_tokens(messages: Any) -> int:
        """Estimate token count: total characters / 4."""
        if not messages:
            return 0
        total_chars = 0
        if isinstance(messages, str):
            total_chars = len(messages)
        elif isinstance(messages, list):
            for m in messages:
                if isinstance(m, dict):
                    content = m.get("content", "")
                    if isinstance(content, str):
                        total_chars += len(content)
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and "text" in part:
                                total_chars += len(str(part["text"]))
                            elif isinstance(part, str):
                                total_chars += len(part)
                elif isinstance(m, str):
                    total_chars += len(m)
        return total_chars // 4

    @staticmethod
    def estimate_recent_tokens(messages: List[Dict[str, Any]], last_n: int = 10) -> int:
        """Estimate tokens from last N messages as specified in trigger rules."""
        if not messages or not isinstance(messages, list):
            return 0
        recent = messages[-last_n:] if len(messages) > last_n else messages
        return TokenBudget.estimate_tokens(recent)

    @staticmethod
    def check_threshold(token_count: int) -> str:
        """Return trigger status: 'save', 'compress', 'warn', or 'ok'."""
        if token_count > TokenBudget.SAVE_THRESHOLD:
            return "save"
        if token_count > TokenBudget.COMPRESS_THRESHOLD:
            return "compress"
        if token_count > TokenBudget.WARNING_THRESHOLD:
            return "warn"
        return "ok"


DAYS_MONTHS_CURRENCY_KEYWORDS = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "percent", "dollar", "dollars", "usd", "eur", "gbp", "yen"
}


def _is_key_number_line(cleaned_line: str) -> bool:
    """Check if a line contains digits, percent, currency, or dates, and exclude questions/opinions."""
    s_lower = cleaned_line.lower()

    # Exclude questions
    if cleaned_line.endswith("?") or s_lower.startswith(("what", "why", "how", "when", "where", "who", "is ", "are ", "can ", "could ", "should ", "would ")):
        return False

    has_digit = bool(re.search(r'\d+', cleaned_line))
    has_percent = "%" in cleaned_line or "percent" in s_lower
    has_currency = bool(re.search(r'[$€£¥]|usd|eur|dollar', s_lower))
    has_date_or_keyword = any(k in s_lower for k in DAYS_MONTHS_CURRENCY_KEYWORDS)

    if not (has_digit or has_percent or has_currency or has_date_or_keyword):
        return False

    # Exclude generic opinions without digits
    generic_opinions = ["i think", "i believe", "in my opinion", "seems like", "maybe"]
    if any(op in s_lower for op in generic_opinions) and not has_digit:
        return False

    return True


def _is_meaningful_statement(s: str) -> bool:
    """Check if statement is self-contained and meaningful (>12 chars and at least 2 words)."""
    cleaned = _clean_statement(s)
    words = cleaned.split()
    if len(cleaned) < 12 or len(words) < 2:
        return False
    vague_fragments = {
        "good fix", "critical next step", "critical step", "next step",
        "todo item", "small fix", "bug fix", "good progress"
    }
    if cleaned.lower() in vague_fragments:
        return False
    return True


def _redact_conversation(conversation: List[Any]) -> Tuple[List[Any], int]:
    """Copy of the conversation with secret-like values redacted, plus the redaction count."""
    total = 0
    out = []
    for msg in conversation:
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            text, n = redact_secrets(msg["content"])
            total += n
            msg = {**msg, "content": text}
        elif isinstance(msg, str):
            msg, n = redact_secrets(msg)
            total += n
        out.append(msg)
    return out, total


def compress_context(
    backend: Any,
    conversation: List[Dict[str, Any]],
    keep_last_n: int = 5
) -> str:
    """
    TOOL 1: cognicore_compress_context
    Compress older messages into a dense, structured summary without LLM calls and store in SQLite.
    """
    if not conversation or not isinstance(conversation, list):
        res = {
            "summary": "No conversation messages provided to compress.",
            "messages_compressed": 0,
            "tokens_before": 0,
            "tokens_after": 0,
            "compression_ratio": "1x",
            "stored_memory_id": None
        }
        return json.dumps(res, indent=2)

    tokens_before = TokenBudget.estimate_tokens(conversation)
    conversation, redactions = _redact_conversation(conversation)

    # Separate older messages vs verbatim recent messages.
    # keep_last_n <= 0 means "compress everything" (conversation[:-0] would be empty).
    if 0 < keep_last_n < len(conversation):
        older_messages = conversation[:-keep_last_n]
        recent_messages = conversation[-keep_last_n:]
    else:
        older_messages = conversation
        recent_messages = []

    messages_compressed = len(older_messages)

    # Calculate input text size in characters for 40% hard limit check
    input_text_len = sum(
        len(m.get("content", "")) if isinstance(m, dict) else len(str(m))
        for m in older_messages
    )

    project_status = ""
    key_numbers = []
    decisions = []
    problems_solved = []
    next_steps = []
    ideas_discussed = []
    code_blocks = []

    seen_normalized = set()

    for msg in older_messages:
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        if not isinstance(content, str):
            continue

        # Extract code blocks verbatim
        found_code = re.findall(r'```[\s\S]*?```', content)
        for cb in found_code:
            cb_clean = cb.strip()
            if cb_clean not in code_blocks:
                code_blocks.append(cb_clean)

        # Remove code blocks before sentence extraction
        text_without_code = re.sub(r'```[\s\S]*?```', ' ', content)
        sentences = _extract_sentences(text_without_code)

        for sent in sentences:
            cleaned = _clean_statement(sent)
            norm = _normalize_sentence(cleaned)
            if not norm or norm in seen_normalized or not _is_meaningful_statement(sent):
                continue

            sl = cleaned.lower()

            # 1. Key numbers / metrics (Strict Bug 2 rules)
            is_number_fact = _is_key_number_line(cleaned)
            
            # 2. Decisions made
            is_decision = any(w in sl for w in ["decid", "agreed", "chose", "opted", "selected", "will use", "architecture", "design choice", "settled on", "going with", "determined"])
            
            # 3. Problems solved
            is_problem_solved = any(w in sl for w in ["fixed", "solv", "resolv", "bugfix", "repaired", "patched", "fix for", "solution", "working now", "corrected"])
            
            # 4. Next steps
            is_next_step = any(w in sl for w in ["next step", "todo", "action item", "will implement", "plan to", "need to", "must do", "critical next", "deploy", "upcoming", "priority"])
            
            # 5. Ideas discussed
            is_idea = any(w in sl for w in ["proposal", "propose", "suggest", "idea", "concept", "discuss", "feature", "potential", "strategy", "explore"])

            # 6. Project status detection
            if not project_status and any(w in sl for w in ["project", "status", "building", "working on", "goal is", "cognicore", "mcp server", "system"]):
                project_status = cleaned

            added = False
            if is_number_fact:
                key_numbers.append(cleaned)
                added = True
            if is_decision:
                decisions.append(cleaned)
                added = True
            if is_problem_solved:
                problems_solved.append(cleaned)
                added = True
            if is_next_step:
                next_steps.append(cleaned)
                added = True
            if is_idea and not added:
                ideas_discussed.append(cleaned)
                added = True

            if added:
                seen_normalized.add(norm)

    if not project_status:
        for msg in older_messages:
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            if isinstance(content, str) and content.strip():
                sents = _extract_sentences(content)
                if sents:
                    project_status = _clean_statement(sents[0])
                    break
    def _build_summary(t_after_estimate: int, max_items: int = 10, include_all_sections: bool = True) -> str:
        lines = [f"COMPRESSED CONTEXT ({messages_compressed} messages → {t_after_estimate} tokens):\n"]
        if project_status:
            lines.append(f"Project status: {project_status}")

        if include_all_sections:
            if key_numbers:
                lines.append("\nKey numbers:")
                for kn in key_numbers[:max_items]:
                    lines.append(f"- {kn}")

            if decisions:
                lines.append("\nDecisions made:")
                for d in decisions[:max_items]:
                    lines.append(f"- {d}")

            if problems_solved:
                lines.append("\nProblems solved:")
                for ps in problems_solved[:max_items]:
                    lines.append(f"- {ps}")

            if next_steps:
                lines.append("\nNext steps:")
                for ns in next_steps[:max_items]:
                    lines.append(f"- {ns}")

            if ideas_discussed:
                lines.append("\nIdeas discussed:")
                for id_item in ideas_discussed[:max_items]:
                    lines.append(f"- {id_item}")
        else:
            # Minimal summary: Project status + Next steps only
            if next_steps:
                lines.append("\nNext steps:")
                for ns in next_steps[:max_items]:
                    lines.append(f"- {ns}")

        if code_blocks and include_all_sections:
            lines.append("\nCode artifacts preserved:\n" + "\n\n".join(code_blocks[:2]))

        return "\n".join(lines)

    # Initial draft
    initial_draft = _build_summary(0, max_items=10, include_all_sections=True)

    # BUG 1 Check & Enforce Hard Limit: MAX 40% of input size
    # For large conversations, enforce 40% limit. Set lower bound to 500 to allow complete structured headers.
    max_allowed_len = max(500, int(input_text_len * 0.4)) if input_text_len > 0 else len(initial_draft)

    summary_text = initial_draft
    if len(summary_text) > max_allowed_len:
        # Step 1: Reduce item count per section
        for items_limit in [5, 3, 2, 1]:
            candidate = _build_summary(0, max_items=items_limit, include_all_sections=True)
            if len(candidate) <= max_allowed_len:
                summary_text = candidate
                break
        else:
            # Step 2: Keep ONLY Project status + Next steps
            candidate_minimal = _build_summary(0, max_items=3, include_all_sections=False)
            if len(candidate_minimal) <= max_allowed_len:
                summary_text = candidate_minimal
            else:
                candidate_one = _build_summary(0, max_items=1, include_all_sections=False)
                summary_text = candidate_one if len(candidate_one) <= max_allowed_len else candidate_one[:max_allowed_len]

    tokens_after = TokenBudget.estimate_tokens([{"content": summary_text}] + recent_messages)
    # Re-inject accurate tokens_after count into final header
    summary_text = summary_text.replace("→ 0 tokens", f"→ {tokens_after} tokens")

    # Store summary in CogniCore memory
    entry = MemoryEntry(
        text=summary_text,
        category="conversation_summary",
        memory_type="semantic",
        creation_reason="cognicore_compress_context"
    )
    stored_id = backend.store(entry)

    # Report the real ratio. Very short inputs can come out larger than they went
    # in (the section headers cost more than they save); say so instead of "1x".
    ratio_val = round(tokens_before / max(1, tokens_after), 1)

    result = {
        "summary": summary_text,
        "messages_compressed": messages_compressed,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "compression_ratio": f"{ratio_val:g}x",
        "expanded": tokens_after > tokens_before,
        "redactions": redactions,
        "stored_memory_id": stored_id
    }
    return json.dumps(result, indent=2)



DEFAULT_RESUME_TOKENS = 1500
# Below this the header, provenance and "more" pointer leave no room for the goal.
MIN_RESUME_TOKENS = {"text": 120, "json": 200}


def _summary_text(session_name: str, timestamp: float, handoff: Dict[str, Any], facts_stored: int) -> str:
    lines = [f"Session '{session_name}' ({datetime.fromtimestamp(timestamp).strftime('%d %b %Y')}):"]
    if handoff["goal"]:
        lines.append(f"- Goal: {handoff['goal']}")
    for d in [d for d in handoff["decisions"] if d["status"] == "made"][:3]:
        lines.append(f"- Decided: {d['what']}")
    for t in [t for t in handoff["open_tasks"] if t["status"] == "open"][:3]:
        lines.append(f"- Action: {t['task']}")
    if len(lines) == 1:
        lines.append(f"- Recorded {facts_stored} facts across conversation.")
    return "\n".join(lines)


def _memory_rows(handoff: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """Searchable rows for a handoff: (text, category, creation_reason), each statement once."""
    rows: List[Tuple[str, str, str]] = []
    if handoff["goal"]:
        rows.append((handoff["goal"], "goals", "session_goal"))
    if handoff["current_state"]:
        rows.append((handoff["current_state"], auto_categorize(handoff["current_state"])[0], "session_state"))
    for d in handoff["decisions"]:
        text = f"Rejected: {d['what']}" if d["status"] == "rejected" else d["what"]
        if d["why"]:
            text += f" (why: {d['why']})"
        rows.append((text, "decisions", "session_decision"))
    for t in handoff["open_tasks"]:
        rows.append((t["task"], "action_items", "session_action"))
    for fact in handoff["key_facts"]:
        for atom in decompose(fact):
            rows.append((atom, auto_categorize(atom)[0], "session_fact"))
    for c in handoff["code"]:
        head = f"{c['context']}\n" if c["context"] else ""
        rows.append((f"{head}```{c['lang']}\n{c['code']}\n```", "code_snippets", "session_code"))

    seen, unique = set(), []
    for text, cat, reason in rows:
        key = _normalize_sentence(text)
        if key and key not in seen:
            seen.add(key)
            unique.append((text, cat, reason))
    return unique


def _existing_ids_by_text(conn, texts: List[str]) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for i in range(0, len(texts), 500):
        chunk = texts[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT entry_id, text FROM memory_entries WHERE text IN ({placeholders})", chunk  # nosec B608 - placeholders only
        ).fetchall():
            found.setdefault(row["text"], str(row["entry_id"]))
    return found


def save_session(
    backend: Any,
    conversation: Optional[List[Dict[str, Any]]] = None,
    session_name: Optional[str] = None,
    agent: str = "",
    client: str = "",
    goal: str = "",
    current_state: str = "",
    environment: Any = None,
    supplied_handoff: Any = None,
) -> str:
    """
    TOOL 2: cognicore_save_session
    Save a conversation as a versioned handoff (``cognicore.handoff/v1``) plus
    searchable memory rows, so another agent can resume the work.

    ``supplied_handoff`` lets the agent write the handoff itself (goal,
    decisions, open_tasks, key_facts, files) instead of, or on top of,
    sending the conversation. That is the token-efficient path: its cost
    does not grow with the conversation.

    Secrets are redacted, chatter is dropped, each statement is stored once
    (statements already in memory are linked, not copied), and saving the
    same content again returns the existing session.
    """
    if not conversation or not isinstance(conversation, list):
        conversation = []
    hashed = list(conversation)
    if supplied_handoff:
        canonical = supplied_handoff
        if isinstance(canonical, str):
            try:
                canonical = json.loads(canonical)  # same content as a dict or a JSON string hashes the same
            except json.JSONDecodeError:
                pass
        hashed.append({"role": "handoff", "content": json.dumps(canonical, sort_keys=True, default=str)})
    conv_hash = conversation_hash(hashed)

    if hashed:
        with backend._get_conn() as conn:
            existing = conn.execute(
                "SELECT s.session_id, m.text FROM sessions s LEFT JOIN memory_entries m "
                "ON m.entry_id = s.summary_memory_id WHERE s.conversation_hash = ? LIMIT 1",
                (conv_hash,),
            ).fetchone()
        if existing:
            return _compact_json({
                "session_id": existing["session_id"],
                "memories_stored": 0,
                "decisions_saved": 0,
                "action_items_saved": 0,
                "code_snippets_saved": 0,
                "summary": "Already saved; returning the existing session.",
                "deduplicated": True,
            })

    timestamp = time.time()
    dt_str = datetime.fromtimestamp(timestamp).strftime("%Y%m%d_%H%M%S")
    # The random suffix keeps two agents saving in the same second from
    # overwriting each other's session row.
    session_id = f"sess_{dt_str}_{uuid.uuid4().hex[:8]}"

    if not session_name:
        date_pretty = datetime.fromtimestamp(timestamp).strftime("%b %d, %Y")
        session_name = f"Session {date_pretty}"

    try:
        handoff = build_handoff(
            conversation, session_id, session_name, timestamp,
            agent=agent, client=client, goal=goal, current_state=current_state, environment=environment,
            supplied=supplied_handoff,
        )
    except HandoffInputError as e:
        return _compact_json({"error": str(e)})
    rows = _memory_rows(handoff)

    with backend._get_conn() as conn:
        existing_ids = _existing_ids_by_text(conn, [text for text, _, _ in rows])
    new_rows = [r for r in rows if r[0] not in existing_ids]
    entries = [
        MemoryEntry(
            text=text,
            category=cat,
            session_id=session_id,
            timestamp=timestamp,
            creation_reason=reason,
            source_agent=agent or "",
        )
        for text, cat, reason in new_rows
    ]
    summary_text = _summary_text(session_name, timestamp, handoff, len(new_rows))
    entries.append(MemoryEntry(
        text=summary_text,
        category="session_summary",
        session_id=session_id,
        timestamp=timestamp,
        creation_reason="session_summary",
        source_agent=agent or "",
    ))
    if hasattr(backend, "store_many"):
        new_ids = backend.store_many(entries)
    else:
        new_ids = [backend.store(e) for e in entries]
    summary_id = new_ids[-1]
    linked_ids = list(new_ids) + list(existing_ids.values())

    handoff_json = json.dumps(handoff, ensure_ascii=False)
    with backend._get_conn() as conn:
        conn.execute("""
            INSERT INTO sessions (
                session_id, name, timestamp, message_count, total_tokens_estimated, summary_memory_id,
                handoff_json, conversation_hash, agent, client
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, session_name, timestamp, len(conversation), TokenBudget.estimate_tokens(conversation),
              summary_id, handoff_json, conv_hash if hashed else None, agent or "", client or ""))
        conn.executemany(
            "INSERT OR IGNORE INTO session_memories (session_id, memory_id) VALUES (?, ?)",
            [(session_id, int(mid)) for mid in linked_ids if str(mid).isdigit()],
        )

    def _count(category: str) -> int:
        return sum(1 for _, cat, _ in new_rows if cat == category)

    # The agent already knows what it saved, so the reply is ids and counts only.
    n_tasks = sum(1 for t in handoff["open_tasks"] if t["status"] == "open")
    res = {
        "session_id": session_id,
        "memories_stored": len(new_rows),
        "decisions_saved": _count("decisions"),
        "action_items_saved": _count("action_items"),
        "code_snippets_saved": _count("code_snippets"),
        "summary": f"Saved '{session_name}': {len(handoff['decisions'])} decisions, {n_tasks} open tasks, "
                   f"{len(handoff['key_facts'])} facts.",
        "schema": SCHEMA_VERSION,
        "already_in_memory": len(existing_ids),
        "redactions": handoff["redactions"],
        "deduplicated": False,
    }
    return _compact_json(res)


def _compact_json(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


_STOPWORDS = {
    "the", "and", "for", "with", "what", "how", "was", "are", "our", "this", "that", "from", "about",
    "work", "working", "continue", "resume", "session", "where", "were", "left", "off",
}


def _query_score(query: str, doc: Dict[str, Any]) -> int:
    text = handoff_text_for_matching(doc)
    goal = (doc.get("goal") or "").lower()
    terms = {t for t in re.findall(r"[a-z0-9][a-z0-9_.\-/]*", query.lower())
             if t not in _STOPWORDS and (len(t) >= 3 or t.isdigit())}
    return sum((2 if t in goal else 1) for t in terms if t in text)


def _load_handoff(backend: Any, row: Any) -> Dict[str, Any]:
    raw = row["handoff_json"] if "handoff_json" in row.keys() else None
    if raw:
        try:
            doc = json.loads(raw)
            if not validate_handoff(doc):
                return doc
            logger.warning("Invalid handoff for %s; rebuilding from memory rows", row["session_id"])
        except json.JSONDecodeError:
            logger.warning("Unreadable handoff for %s; rebuilding from memory rows", row["session_id"])
    with backend._get_conn() as conn:
        mem_rows = conn.execute(
            "SELECT text, category FROM memory_entries WHERE session_id = ? ORDER BY entry_id",
            (row["session_id"],),
        ).fetchall()
    return handoff_from_legacy([dict(r) for r in mem_rows], row["session_id"], row["name"], row["timestamp"] or 0.0)


def _relevant_experiences(backend: Any, query: str, environment: Any, top_k: int = 3) -> List[Dict[str, Any]]:
    """Verified experiences relevant to the resumed work (empty if none or unavailable)."""
    if not query:
        return []
    try:
        from cognicore.experience.manager import ExperienceManager
        from cognicore.experience.schema import EnvironmentContext
        env = normalize_environment(environment)
        result = ExperienceManager(backend).retrieve(
            query=query,
            current_env=EnvironmentContext.from_dict(env) if env else None,
            include_failures=False,
            require_verified=True,
            top_k=top_k,
        )
    except Exception as e:  # experiences are optional context for a resume
        logger.debug("Experience lookup skipped: %s", e)
        return []
    return [
        {"experience_id": e.experience_id, "task": e.task, "solution": e.solution,
         "status": str(getattr(e.verification_status, "value", e.verification_status))}
        for e in result.experiences
    ]


def _render(fmt: str, included: Dict[str, Any], omitted: Dict[str, int], provenance: Dict[str, Any],
            environment: Dict[str, Any], max_tokens_applied: Optional[int] = None) -> str:
    more = ""
    if omitted:
        counts = ", ".join(f"{n} {s}" for s, n in omitted.items())
        more = (f"More available ({counts}): cognicore_resume_session("
                f"session_id='{provenance.get('session_id')}', section=<name>)")
    if fmt == "json":
        doc = {"schema": SCHEMA_VERSION, "provenance": provenance, **included}
        if max_tokens_applied:
            doc["max_tokens_applied"] = max_tokens_applied
        if environment:
            doc["environment"] = environment
        if omitted:
            doc["omitted"] = omitted
            doc["more"] = more
        body = json.dumps(doc, ensure_ascii=False)
        return json.dumps({**doc, "tokens": TokenBudget.estimate_tokens(body)}, ensure_ascii=False)
    brief = render_text(included, provenance, more)
    return f"{brief}\n\n=== END CONTEXT | {TokenBudget.estimate_tokens(brief)} tokens ==="


def _drop_last_item(included: Dict[str, Any], omitted: Dict[str, int]) -> bool:
    """Remove the lowest-priority included item. Returns False when nothing is left to drop."""
    for section in reversed(SECTION_PRIORITY):
        value = included.get(section)
        if not value:
            continue
        if isinstance(value, list) and len(value) > 1:
            value.pop()
        else:
            del included[section]
        omitted[section] = omitted.get(section, 0) + 1
        return True
    return False


def resume_session(
    backend: Any,
    query: Optional[str] = None,
    last_n_sessions: int = 3,
    include_action_items: bool = True,
    max_tokens: Optional[int] = None,
    format: str = "text",
    session_id: Optional[str] = None,
    section: Optional[str] = None,
    environment: Any = None,
) -> str:
    """
    TOOL 3: cognicore_resume_session
    Reconstruct context from past sessions at the start of a new conversation.

    The best-matching session (by ``query``, else the latest) is returned in
    priority order — goal, current state, environment warnings, open tasks,
    decisions, key facts, files, exact values, experiences, code, other
    sessions — until ``max_tokens`` is used. Whatever did not fit is listed
    so it can be fetched with ``session_id`` + ``section``.
    """
    fmt = "json" if str(format).lower() == "json" else "text"
    if section and section not in SECTIONS and section != "all":
        return f"Error: section must be one of: all, {', '.join(SECTIONS)}."

    with backend._get_conn() as conn:
        if session_id:
            rows = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchall()
        else:
            limit = 200 if query and query.strip() else max(1, last_n_sessions)
            rows = conn.execute("SELECT * FROM sessions ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        if session_id:
            return f"Error: no saved session with id {session_id!r}."
        return _resume_without_sessions(backend, query, include_action_items)

    docs = [_load_handoff(backend, row) for row in rows]
    if query and query.strip() and not session_id:
        docs.sort(key=lambda d: -_query_score(query, d))  # stable: ties keep newest first
    docs = docs[:max(1, last_n_sessions)]
    primary, others = docs[0], docs[1:]
    env_saved = primary.get("environment") or {}

    sections: Dict[str, Any] = {
        "goal": primary.get("goal", ""),
        "current_state": primary.get("current_state", ""),
        "environment_warnings": environment_warnings(env_saved, environment),
        "open_tasks": [t for t in primary.get("open_tasks", []) if t.get("status") == "open"]
        if include_action_items else [],
        "decisions": list(reversed(primary.get("decisions", []))),  # most recent first
        "key_facts": primary.get("key_facts", []),
        "files": primary.get("files", []),
        "experiences": _relevant_experiences(backend, (query or "").strip() or primary.get("goal", ""), environment),
        "code": primary.get("code", []),
        "other_sessions": [
            {
                "session_id": d["provenance"]["session_id"],
                "session_name": d["provenance"].get("session_name", ""),
                "created_at": d["provenance"].get("created_at", ""),
                "goal": d.get("goal", ""),
                "current_state": d.get("current_state", ""),
                "open_tasks": [t["task"] for t in d.get("open_tasks", []) if t.get("status") == "open"][:3],
                "decisions": [x["what"] for x in d.get("decisions", []) if x.get("status") == "made"][-3:],
                "key_facts": d.get("key_facts", [])[:5],
            }
            for d in others
        ],
    }
    provenance = primary["provenance"]

    if section:
        # On-demand retrieval of what a budgeted resume left out.
        if section in ("provenance", "environment"):
            return json.dumps(provenance if section == "provenance" else env_saved, indent=2)
        sections["exact_values"] = primary.get("exact_values", [])
        if section != "all":
            sections = {section: sections.get(section)}
        budget = max_tokens if max_tokens and max_tokens > 0 else None
    else:
        budget = max_tokens if max_tokens and max_tokens > 0 else DEFAULT_RESUME_TOKENS
    if budget is not None:
        budget = max(budget, MIN_RESUME_TOKENS[fmt])

    def render(inc: Dict[str, Any], om: Dict[str, int]) -> str:
        return _render(fmt, inc, om, provenance, env_saved if fmt == "json" else {},
                       max_tokens_applied=budget if max_tokens and budget != max_tokens else None)

    cost = json_cost if fmt == "json" else text_cost
    fixed = TokenBudget.estimate_tokens(render({}, {})) + 30

    included, omitted = select_for_budget(sections, budget, cost, fixed)
    if not section:
        # List files (text brief) and exact values only when the selected text does not
        # already mention them: cheap insurance without repeating what is on screen.
        visible = json.dumps({k: v for k, v in included.items() if k not in ("files", "exact_values")},
                             ensure_ascii=False).lower()
        if fmt == "text":
            sections["files"] = [f for f in primary.get("files", []) if f.lower() not in visible]
            sections["code"] = [{**c, "context": ""} if c.get("context") and c["context"].lower() in visible else c
                                for c in primary.get("code", [])]
        shown = visible + " " + " ".join(sections["files"] or []).lower()
        sections["exact_values"] = [v for v in primary.get("exact_values", []) if v["value"].lower() not in shown]
        included, omitted = select_for_budget(sections, budget, cost, fixed)

    out = render(included, omitted)
    while budget is not None and TokenBudget.estimate_tokens(out) > budget and _drop_last_item(included, omitted):
        out = render(included, omitted)
    return out


def _resume_without_sessions(backend: Any, query: Optional[str], include_action_items: bool) -> str:
    """Pre-session behaviour: build a brief from loose memories when no session was ever saved."""
    lines = ["=== COGNICORE CONTEXT RESUME ===\n"]

    fact_results = backend.search(query if query and query.strip() else "", top_k=5)
    if fact_results:
        lines.append("Key facts:")
        for r in fact_results[:5]:
            lines.append(f"  - {r.entry.text}")
        lines.append("")

    if include_action_items:
        action_results = backend.search("", top_k=5, category="action_items")
        if action_results:
            lines.append("Pending action items:")
            for r in action_results[:5]:
                lines.append(f"  - {r.entry.text}")
            lines.append("")

    dec_results = backend.search("", top_k=3, category="decisions")
    if dec_results:
        lines.append("Recent decisions:")
        for r in dec_results[:3]:
            lines.append(f"  - {r.entry.text}")
        lines.append("")

    brief_text = "\n".join(lines).strip()
    token_count = TokenBudget.estimate_tokens(brief_text)

    return f"{brief_text}\n\n=== END CONTEXT | {token_count} tokens ==="


def handle_token_triggers(
    backend: Any,
    conversation: Optional[List[Dict[str, Any]]] = None,
    current_response: str = ""
) -> str:
    """
    Check TokenBudget thresholds on every tool call and apply triggers.
    """
    if not conversation or not isinstance(conversation, list):
        return current_response

    token_count = TokenBudget.estimate_recent_tokens(conversation, last_n=10)
    status = TokenBudget.check_threshold(token_count)

    if status == "save":
        save_session(backend, conversation)
        return "Session saved. Start fresh conversation and run cognicore_resume_session to continue."
    elif status == "compress":
        compressed_json = compress_context(backend, conversation)
        try:
            data = json.loads(compressed_json)
            summary_msg = f"\n\n[Auto-Compressed Context ({data.get('compression_ratio')})]:\n{data.get('summary')}"
            return current_response + summary_msg
        except Exception:
            return current_response + f"\n\n[Auto-Compressed Context]:\n{compressed_json}"
    elif status == "warn":
        warn_msg = "\n\n[WARNING] Context at 75% -- consider running cognicore_save_session soon"
        return current_response + warn_msg

    return current_response

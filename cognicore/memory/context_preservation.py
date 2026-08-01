"""
Context Preservation System for CogniCore Memory.

Provides automatic context compression, session archiving, and session resumption
to solve token exhaustion in long LLM conversations without requiring additional LLM API calls.
"""

import re
import json
import time
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

from cognicore.memory.base import MemoryEntry, MemoryScope
from cognicore.memory.decompose import decompose
from cognicore.memory.categorize import auto_categorize

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


def _extract_sentences(text: str) -> List[str]:
    """Split text into sentences cleanly, protecting decimal numbers."""
    if not text:
        return []
    # Protect decimal numbers (e.g. 98.2, 98. 2) so dots aren't treated as sentence boundaries
    protected_text = re.sub(r'(\d+)\.\s*(\d+)', r'\1__DOT__\2', text)
    
    # Split on periods, exclamation marks, question marks followed by space/newline, or newlines
    raw_sentences = re.split(r'(?<=[.!?])\s+|\n+', protected_text)
    
    # Restore decimal points
    restored = [s.replace("__DOT__", ".").strip() for s in raw_sentences]
    return [s for s in restored if s and len(s) > 5]


def _normalize_sentence(s: str) -> str:
    """Normalize sentence for deduplication."""
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _clean_statement(s: str) -> str:
    """Clean bullet markers and whitespace from sentence while keeping full content."""
    cleaned = re.sub(r'^(?:[-*•]|\d+\.)\s*', '', s.strip())
    return cleaned.strip()


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
    
    # Separate older messages vs verbatim recent messages
    if len(conversation) > keep_last_n:
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
    if not project_status:
        project_status = "Active development on CogniCore MCP server and memory systems."

    def _build_summary(t_after_estimate: int, max_items: int = 10, include_all_sections: bool = True) -> str:
        lines = [f"COMPRESSED CONTEXT ({messages_compressed} messages → {t_after_estimate} tokens):\n"]
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

    ratio_val = max(1, round(tokens_before / max(1, tokens_after)))

    result = {
        "summary": summary_text,
        "messages_compressed": messages_compressed,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "compression_ratio": f"{ratio_val}x",
        "stored_memory_id": stored_id
    }
    return json.dumps(result, indent=2)



def save_session(
    backend: Any,
    conversation: List[Dict[str, Any]],
    session_name: Optional[str] = None
) -> str:
    """
    TOOL 2: cognicore_save_session
    Save everything important at the end of a conversation into SQLite session tables.
    """
    timestamp = time.time()
    dt_str = datetime.fromtimestamp(timestamp).strftime("%Y%m%d_%H%M%S")
    session_id = f"sess_{dt_str}"
    
    if not session_name:
        date_pretty = datetime.fromtimestamp(timestamp).strftime("%b %d, %Y")
        session_name = f"Session {date_pretty}"

    if not conversation or not isinstance(conversation, list):
        conversation = []

    memories_stored = 0
    decisions_saved = 0
    action_items_saved = 0
    code_snippets_saved = 0
    stored_ids = []

    decisions_list = []
    actions_list = []

    for msg in conversation:
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        if not isinstance(content, str):
            continue

        # 1. Extract atomic facts
        facts = decompose(content)
        for fact in facts:
            cat, _ = auto_categorize(fact)
            entry = MemoryEntry(
                text=fact,
                category=cat,
                session_id=session_id,
                timestamp=timestamp,
                creation_reason="session_fact"
            )
            mid = backend.store(entry)
            stored_ids.append(mid)
            memories_stored += 1

        # 2. Extract decisions
        sentences = _extract_sentences(content)
        for sent in sentences:
            sl = sent.lower()
            if any(w in sl for w in ["decided", "agree", "will use", "chose", "decision", "opted for", "let's use"]):
                entry = MemoryEntry(
                    text=sent,
                    category="decisions",
                    session_id=session_id,
                    timestamp=timestamp,
                    creation_reason="session_decision"
                )
                mid = backend.store(entry)
                stored_ids.append(mid)
                decisions_saved += 1
                decisions_list.append(sent)

            # 4. Extract action items / next steps
            if any(w in sl for w in ["todo", "next step", "action item", "- [ ]", "need to", "must do", "pending"]):
                entry = MemoryEntry(
                    text=sent,
                    category="action_items",
                    session_id=session_id,
                    timestamp=timestamp,
                    creation_reason="session_action"
                )
                mid = backend.store(entry)
                stored_ids.append(mid)
                action_items_saved += 1
                actions_list.append(sent)

        # 3. Extract code snippets with context
        code_blocks = re.findall(r'`{1,3}(\w*)\n([\s\S]*?)`{1,3}', content)
        if code_blocks:
            text_before = re.split(r'`{1,3}', content)[0].strip()
            sents = _extract_sentences(text_before)
            preceding = sents[-1] if sents else text_before
            for lang, code_body in code_blocks:
                snippet_text = f"{preceding}\n```{lang}\n{code_body.strip()}\n```" if preceding else f"```{lang}\n{code_body.strip()}\n```"
                entry = MemoryEntry(
                    text=snippet_text,
                    category="code_snippets",
                    session_id=session_id,
                    timestamp=timestamp,
                    creation_reason="session_code"
                )
                mid = backend.store(entry)
                stored_ids.append(mid)
                code_snippets_saved += 1

    # 5. Create session summary memory
    summary_lines = [f"Session '{session_name}' ({datetime.fromtimestamp(timestamp).strftime('%d %b %Y')}):"]
    if decisions_list:
        summary_lines.append(f"- Decided: {decisions_list[0]}")
    if actions_list:
        summary_lines.append(f"- Action: {actions_list[0]}")
    if not decisions_list and not actions_list:
        summary_lines.append(f"- Recorded {memories_stored} facts across conversation.")

    summary_text = "\n".join(summary_lines)
    summary_entry = MemoryEntry(
        text=summary_text,
        category="session_summary",
        session_id=session_id,
        timestamp=timestamp,
        creation_reason="session_summary"
    )
    summary_id = backend.store(summary_entry)
    stored_ids.append(summary_id)

    # Store session metadata in SQLite tables
    total_tokens = TokenBudget.estimate_tokens(conversation)
    with backend._get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO sessions (
                session_id, name, timestamp, message_count, total_tokens_estimated, summary_memory_id
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (session_id, session_name, timestamp, len(conversation), total_tokens, summary_id))

        for mid in stored_ids:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO session_memories (session_id, memory_id) VALUES (?, ?)
                """, (session_id, int(mid) if str(mid).isdigit() else 0))
            except Exception:
                pass
            try:
                conn.execute("UPDATE memory_entries SET session_id = ? WHERE entry_id = ?", (session_id, mid))
            except Exception:
                pass

    res = {
        "session_id": session_id,
        "memories_stored": memories_stored,
        "decisions_saved": decisions_saved,
        "action_items_saved": action_items_saved,
        "code_snippets_saved": code_snippets_saved,
        "summary": summary_text
    }
    return json.dumps(res, indent=2)


def resume_session(
    backend: Any,
    query: Optional[str] = None,
    last_n_sessions: int = 3,
    include_action_items: bool = True
) -> str:
    """
    TOOL 3: cognicore_resume_session
    Reconstruct context from past sessions instantly at the start of a new conversation.
    """
    lines = ["=== COGNICORE CONTEXT RESUME ===\n"]

    with backend._get_conn() as conn:
        session_rows = conn.execute("""
            SELECT session_id, name, timestamp, summary_memory_id
            FROM sessions
            ORDER BY timestamp DESC
            LIMIT ?
        """, (last_n_sessions,)).fetchall()

    if session_rows:
        latest_sess = session_rows[0]
        dt_formatted = datetime.fromtimestamp(latest_sess["timestamp"]).strftime("%d %b %Y")
        lines.append(f"Last session ({dt_formatted}):")
        
        # Fetch session summary memory text
        summary_row = backend._get_conn().execute(
            "SELECT text FROM memory_entries WHERE entry_id = ?", (latest_sess["summary_memory_id"],)
        ).fetchone()
        if summary_row and summary_row["text"]:
            for sl in str(summary_row["text"]).splitlines():
                if sl.strip().startswith("-") or sl.strip().startswith("•"):
                    lines.append(f"  {sl.strip().replace('•', '-')}")
                elif sl.strip() and not sl.startswith("Session"):
                    lines.append(f"  - {sl.strip()}")
        lines.append("")

    # 2. Retrieve relevant key facts
    if query and query.strip():
        fact_results = backend.search(query, top_k=5)
    else:
        fact_results = backend.search("", top_k=5)
    
    if fact_results:
        lines.append("Key facts:")
        for r in fact_results[:5]:
            lines.append(f"  - {r.entry.text}")
        lines.append("")

    # 3. Retrieve pending action items
    if include_action_items:
        action_results = backend.search("", top_k=5, category="action_items")
        if action_results:
            lines.append("Pending action items:")
            for r in action_results[:5]:
                lines.append(f"  - {r.entry.text}")
            lines.append("")

    # 4. Retrieve recent decisions
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

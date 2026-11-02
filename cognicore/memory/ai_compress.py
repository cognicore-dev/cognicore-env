"""
Optional AI compression of a session into a handoff, via the Claude API.

Off by default: rule-based capture needs no model calls. Turn on with
``COGNICORE_AI_COMPRESS=auto`` (hooks compress long sessions) or call
:func:`compress_with_ai` directly. Rule-based extraction makes the brief
grow with the conversation, because it keeps every substantive sentence.
A model can summarize long, messy sessions into a much smaller handoff.

Safeguards:
- Detected secrets are redacted before anything is sent.
- Every exact value (id, version, path, number...) in the summary must occur
  in the transcript; items that cite a value the transcript doesn't contain
  are dropped, so an invented value never reaches the next session.
- Any error, refusal or missing credentials returns ``None``. Callers then fall
  back to rule-based capture; a save never fails because of the model.

Environment:
    COGNICORE_AI_COMPRESS     "auto" to enable in the hooks (default: off)
    COGNICORE_AI_MODEL        default claude-opus-5-5; claude-haiku-5-5 is the low-cost option
    COGNICORE_AI_EFFORT       default "low" (summarizing doesn't need deep thinking)
    COGNICORE_AI_MIN_TOKENS   compress only sessions this long, and again only after
                              this many new tokens (default 3000)
Credentials come from the Anthropic SDK's usual sources (ANTHROPIC_API_KEY,
ANTHROPIC_AUTH_TOKEN or an ``ant auth login`` profile).
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from cognicore.memory.handoff import extract_exact_values
from cognicore.memory.redact import redact_secrets

logger = logging.getLogger("cognicore.ai_compress")

DEFAULT_MODEL = "claude-opus-5-5"
# Leave room for the instructions and the answer inside the model's context.
MAX_TRANSCRIPT_TOKENS = 600_000

HANDOFF_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "current_state": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"what": {"type": "string"}, "why": {"type": "string"}},
                "required": ["what", "why"],
                "additionalProperties": False,
            },
        },
        "open_tasks": {"type": "array", "items": {"type": "string"}},
        "key_facts": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["goal", "current_state", "decisions", "open_tasks", "key_facts", "files"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You write the handoff for a coding session, so that a different agent can continue the work without the transcript.

Return only what the next agent needs:
- goal: what the work is for, in one sentence.
- current_state: where the work stands now: what is done, what is broken, what is next.
- decisions: each choice that was made, with the reason given. A rejected approach is a decision whose "what" starts with "Rejected:".
- open_tasks: work that is still to do, including anything explicitly deferred.
- key_facts: other facts the next agent would otherwise have to ask for, such as names, deadlines, constraints, environment details and numbers.
- files: files and paths that were changed or are central to the work.

Copy every exact value verbatim: ids, ticket numbers, versions, hashes, paths, URLs, env var names, error codes, numbers with their units, dates and people's names. Never paraphrase or round them. Include only what the transcript states. Leave out greetings, acknowledgements, narration of steps and anything already fully captured by another field. Prefer short, dense items."""


def _transcript_text(conversation: List[Any]) -> str:
    lines = []
    for msg in conversation or []:
        if isinstance(msg, dict):
            role, content = msg.get("role", ""), msg.get("content", "")
        else:
            role, content = "", str(msg)
        if isinstance(content, str) and content.strip():
            lines.append(f"{role or 'message'}: {content.strip()}")
    return "\n\n".join(lines)


def _grounded(item: str, source_lower: str) -> bool:
    """True when every exact value the item cites occurs in the source transcript."""
    return all(v["value"].lower() in source_lower for v in extract_exact_values(item))


def _keep_grounded(summary: Dict[str, Any], source: str) -> Tuple[Dict[str, Any], int]:
    low = source.lower()
    dropped = 0

    def keep(text: str) -> bool:
        nonlocal dropped
        if _grounded(text, low):
            return True
        dropped += 1
        return False

    out: Dict[str, Any] = {}
    for key in ("goal", "current_state"):
        value = (summary.get(key) or "").strip()
        out[key] = value if value and keep(value) else ""
    out["decisions"] = [d for d in summary.get("decisions") or []
                        if d.get("what") and keep(f"{d.get('what', '')} {d.get('why', '')}")]
    for key in ("open_tasks", "key_facts", "files"):
        out[key] = [s for s in summary.get(key) or [] if isinstance(s, str) and s.strip() and keep(s)]
    return out, dropped


def compress_with_ai(
    conversation: List[Any],
    model: Optional[str] = None,
    effort: Optional[str] = None,
    client: Any = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Summarize a conversation into a partial handoff with Claude.

    Returns ``(summary, info)``. ``summary`` uses the shape save_session accepts as
    ``supplied_handoff``, or is ``None`` when the model could not be used. ``info``
    records the model, token usage, and why it was skipped if it was.
    """
    model = model or os.environ.get("COGNICORE_AI_MODEL") or DEFAULT_MODEL
    effort = effort or os.environ.get("COGNICORE_AI_EFFORT") or "low"
    redacted, redactions = redact_secrets(_transcript_text(conversation))
    info: Dict[str, Any] = {"method": "ai", "model": model, "redactions": redactions,
                            "source_tokens": len(redacted) // 4}
    if not redacted.strip():
        return None, {**info, "skipped": "empty"}
    if info["source_tokens"] > MAX_TRANSCRIPT_TOKENS:
        # Not truncated silently: the session keeps rule-based capture instead.
        return None, {**info, "skipped": "too_long"}

    try:
        import anthropic
    except ImportError:
        return None, {**info, "skipped": "anthropic package not installed (pip install anthropic)"}

    try:
        client = client or anthropic.Anthropic()
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"<transcript>\n{redacted}\n</transcript>"}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": HANDOFF_SCHEMA}},
            # On a policy decline, the API retries on a suitable fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as e:
        return None, {**info, "skipped": f"API error {e.status_code}"}
    except anthropic.APIConnectionError:
        return None, {**info, "skipped": "network error"}
    except anthropic.AnthropicError as e:  # e.g. no credentials configured
        return None, {**info, "skipped": f"{type(e).__name__}: {e}"}

    usage = getattr(response, "usage", None)
    info.update(
        served_by=getattr(response, "model", model),
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )
    if response.stop_reason == "refusal":
        return None, {**info, "skipped": "refusal"}
    if response.stop_reason == "max_tokens":
        return None, {**info, "skipped": "summary cut off at max_tokens"}

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        summary = json.loads(text)
    except json.JSONDecodeError:
        return None, {**info, "skipped": "invalid JSON"}
    summary, dropped = _keep_grounded(summary, redacted)
    info["dropped_ungrounded"] = dropped
    return summary, info

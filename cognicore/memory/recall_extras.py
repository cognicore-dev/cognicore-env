"""What a plain relevance search misses: exact values, an agent's own work, true counts.

* :func:`literal_hits` finds statements containing an exact value from the query
  ("PR #227", "fec729d", "0.12.0") verbatim, in memory rows and in saved sessions,
  so an id never comes back as a near miss.
* :func:`agent_sessions` lists the sessions one agent saved.
* :func:`memory_counts` counts every memory (notes and session facts), by category.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from cognicore.memory import brief_filters as bf
from cognicore.memory.handoff import session_dates

# Query tokens that are values, not words: anything with a digit, "#", "/", "_" or an inner dot.
_VALUE = re.compile(r"#?[A-Za-z0-9][\w.#/-]*")


def exact_tokens(query: str) -> List[str]:
    out = []
    for tok in _VALUE.findall(query or ""):
        tok = tok.rstrip(".,;:")
        if len(tok) >= 3 and (re.search(r"\d", tok) or re.search(r"[#/_]|\w\.\w", tok)):
            out.append(tok)
    # "PR #227": the number alone is the value; "#227" matches "PR #227" and "(#227)".
    return out


def literal_hits(backend: Any, query: str, limit: int = 5) -> List[Tuple[str, str, str]]:
    """(text, where, date) for statements that contain every exact value in *query*."""
    tokens = exact_tokens(query)
    if not tokens or not hasattr(backend, "_get_conn"):
        return []
    hits: List[Tuple[str, str, str]] = []
    seen = set()
    like = " AND ".join("text LIKE ?" for _ in tokens)
    srows: List[Any] = []
    with backend._get_conn() as conn:
        rows = conn.execute(
            f"SELECT entry_id, text, category, timestamp FROM memory_entries WHERE {like} "  # nosec B608 - placeholders
            "AND state NOT IN ('archived', 'deleted') ORDER BY timestamp DESC LIMIT ?",
            [f"%{t}%" for t in tokens] + [limit],
        ).fetchall()
        for r in rows:
            key = (r["text"] or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                hits.append((r["text"], f"{r['category']} #{r['entry_id']}", bf.iso_day(r["timestamp"])))
        if len(hits) < limit:
            # Sessions keep statements that have no memory row of their own (e.g. "Done" items saved earlier).
            srows = conn.execute(
                f"SELECT session_id, handoff_json FROM sessions WHERE {like.replace('text', 'handoff_json')} "  # nosec B608
                "ORDER BY timestamp DESC LIMIT 20", [f"%{t}%" for t in tokens]).fetchall()
    for s in srows:
        try:
            doc = json.loads(s["handoff_json"] or "{}")
        except ValueError:
            continue
        day = session_dates(doc.get("provenance") or {})[0]
        for label, items in (("done", doc.get("completed") or []),
                             ("decision", [d.get("what", "") for d in doc.get("decisions") or []]),
                             ("task", [t.get("task", "") for t in doc.get("open_tasks") or []]),
                             ("fact", doc.get("key_facts") or []),
                             ("state", [doc.get("current_state") or ""])):
            for text in items:
                if not isinstance(text, str) or not all(t.lower() in text.lower() for t in tokens):
                    continue
                key = text.strip().lower()
                if key not in seen:
                    seen.add(key)
                    hits.append((text, f"{label} in {s['session_id']}", day))
                if len(hits) >= limit:
                    return hits
    return hits


def agent_sessions(backend: Any, agent: str, limit: int = 5) -> Tuple[int, List[str]]:
    """(count, lines) for the sessions *agent* saved, newest first."""
    if not hasattr(backend, "_get_conn"):
        return 0, []
    with backend._get_conn() as conn:
        rows = conn.execute("SELECT session_id, handoff_json FROM sessions WHERE handoff_json IS NOT NULL "
                            "ORDER BY timestamp DESC LIMIT 500").fetchall()
    lines, n = [], 0
    for r in rows:
        try:
            doc = json.loads(r["handoff_json"])
        except ValueError:
            continue
        if not bf.by_agent(doc, agent):
            continue
        n += 1
        if len(lines) < limit:
            started, updated = session_dates(doc.get("provenance") or {})
            what = doc.get("current_state") or doc.get("goal") or ""
            lines.append(f"  - {r['session_id']} ({started}{', updated ' + updated if updated != started else ''}): "
                         f"{what[:140]}")
    return n, lines


def memory_counts(backend: Any) -> Dict[str, Any]:
    """Every stored memory (remembered notes and facts from saved sessions), by category and kind."""
    with backend._get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memory_entries WHERE state NOT IN ('archived', 'deleted')"
                             ).fetchone()[0]
        cats = conn.execute("SELECT COALESCE(NULLIF(category, ''), 'general') AS c, COUNT(*) AS n FROM memory_entries "
                            "WHERE state NOT IN ('archived', 'deleted') GROUP BY c ORDER BY n DESC").fetchall()
        notes = conn.execute("SELECT COUNT(*) FROM memory_entries WHERE state NOT IN ('archived', 'deleted') "
                             "AND COALESCE(creation_reason, '') NOT LIKE 'session%'").fetchone()[0]
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    return {"total": int(total), "notes": int(notes), "session_facts": int(total) - int(notes),
            "sessions": int(sessions), "categories": [(r["c"], int(r["n"])) for r in cats]}

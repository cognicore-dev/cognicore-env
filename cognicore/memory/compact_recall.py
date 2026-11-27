"""Recall modes on top of search: standard (default), lean and rich.

All three start from ``search``'s own ranking. Tokens are estimated as in the recall
output: about 4 characters per token plus RESULT_OVERHEAD per result for its score, date
and id. Pieces (conversation turns, else paragraphs, else the whole memory) are compared
ignoring case and runs of whitespace.

standard  ``compact_results(base, rest)``: the results search shows (``base``, its top_k)
          with each piece shown once, plus next-ranked memories, added whole, in part of
          the space that saved. Guarantees: everything ``base`` shows is still shown, the
          output never costs more tokens than ``base``, and when nothing repeats the
          output is exactly ``base``.

lean      ``lean_results``: the passages that match the query, not whole memories. Each
          matching turn comes with the turn before it (and the turn after it when that one
          is short), continuing into the neighbouring memory of the same saved session.
          Fills LEAN_SHARE of what ``base`` costs; never more than ``base`` costs.
          Fewer tokens; less surrounding text than the other modes.

rich      the top RICH_FACTOR x top_k results of search, each piece shown once, plus
          extras like standard. Guarantees: everything search's top_k shows is still
          shown, and the output never costs more than search's top RICH_FACTOR x top_k.
          More context; more tokens than standard.

Settings (environment):
    COGNICORE_COMPACT_RECALL      "0": plain search results (all modes off)
    COGNICORE_RECALL_MODE         lean | standard | rich (default standard)
    COGNICORE_RECALL_EXTRA_SPEND  share of the saved tokens extras may use, 0..1 (default 0.5)
    COGNICORE_LEAN_SHARE          share of the standard cost lean fills, 0.1..1 (default 0.6)
"""

import copy
import math
import os
import re
from collections import Counter
from typing import List, Optional, Sequence, Tuple

from cognicore.memory.base import SearchResult

RESULT_OVERHEAD = 12
DEFAULT_EXTRA_SPEND = 0.5
DEFAULT_LEAN_SHARE = 0.6
MODES = ("lean", "standard", "rich")
# Candidates fetched for extras and excerpts: top_k x this.
POOL_FACTOR = 4
# Rich mode keeps search's top RICH_FACTOR x top_k.
RICH_FACTOR = 2
# Lean mode: a turn this short (in tokens) after a matching turn comes along with it.
SHORT_TURN = 60

_SPEAKER = re.compile(r"^[A-Za-z][\w .'-]{0,30}: ")
_PARAGRAPH = re.compile(r"\n\s*\n")
_WORD = re.compile(r"[a-z0-9]+")
# Lean mode scores passages on content words only; questions are full of these.
_STOP_WORDS = frozenset("""
a about above after again all also am an and any are as at be because been before being below
between both but by can could did do does doing done down during each few for from further get
got had has have having he her here hers him his how i if in into is it its itself just me more
most my no nor not now of off on once only or other our ours out over own same she should so
some such than that the their theirs them then there these they this those through to too under
until up us very was we were what when where which while who whom why will with would you your
yours someone something anything thing things really
""".split())


def compact_enabled() -> bool:
    return os.environ.get("COGNICORE_COMPACT_RECALL", "1").strip() != "0"


def _env_share(name: str, default: float, low: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return min(1.0, max(low, value))


def extra_spend() -> float:
    return _env_share("COGNICORE_RECALL_EXTRA_SPEND", DEFAULT_EXTRA_SPEND, 0.0)


def lean_share() -> float:
    return _env_share("COGNICORE_LEAN_SHARE", DEFAULT_LEAN_SHARE, 0.1)


def recall_mode(requested: Optional[str] = None) -> str:
    """The mode to use: *requested* if it names one, else COGNICORE_RECALL_MODE, else standard."""
    for value in (requested, os.environ.get("COGNICORE_RECALL_MODE")):
        value = (value or "").strip().lower()
        if value in MODES:
            return value
    return "standard"


def tokens(text: str) -> int:
    """Rough token count: about 4 characters per token."""
    return (len(text or "") + 3) // 4


def split_pieces(text: str) -> Tuple[List[str], str]:
    """(pieces, separator) for *text*: conversation turns, else paragraphs, else the whole text.

    A turn is a line starting with a speaker label ("user: ", "assistant: ") plus the lines
    after it up to the next label. Joining the pieces with the separator gives back *text*.
    """
    text = text or ""
    lines = text.split("\n")
    if sum(1 for ln in lines if _SPEAKER.match(ln)) >= 2 and _SPEAKER.match(lines[0]):
        turns: List[str] = []
        for ln in lines:
            if _SPEAKER.match(ln) or not turns:
                turns.append(ln)
            else:
                turns[-1] += "\n" + ln
        return turns, "\n"
    if _PARAGRAPH.search(text):
        paragraphs = _PARAGRAPH.split(text)
        if "\n\n".join(paragraphs) == text:  # only when the paragraphs join back exactly
            return paragraphs, "\n\n"
    return [text], ""


def _key(piece: str) -> str:
    return " ".join(piece.lower().split())


def _cost(text: str) -> int:
    return tokens(text) + RESULT_OVERHEAD


def _with_text(result: SearchResult, text: str, source: Optional[str] = None) -> SearchResult:
    if text == result.entry.text and source is None:
        return result
    entry = copy.copy(result.entry)
    entry.text = text
    return SearchResult(entry=entry, score=result.score, source=source or result.source)


# --- standard / rich -------------------------------------------------------------------------

def compact_results(base: Sequence[SearchResult], rest: Sequence[SearchResult] = (),
                    spend: float = DEFAULT_EXTRA_SPEND) -> List[SearchResult]:
    """Standard mode; see the module docstring. Never mutates the given results."""
    cap = sum(_cost(r.entry.text) for r in base)
    seen = set()
    out: List[SearchResult] = []
    used = 0

    def unseen(result):
        pieces, sep = split_pieces(result.entry.text)
        new = [p for p in pieces if p.strip() and _key(p) not in seen]
        return new, sep

    def take(result, new, sep):
        nonlocal used
        text = sep.join(new) if sep else new[0]
        seen.update(_key(p) for p in new)
        out.append(_with_text(result, text))
        used += _cost(text)

    for r in base:
        new, sep = unseen(r)
        if new:
            take(r, new, sep)

    allowance = used + min(1.0, max(0.0, spend)) * (cap - used)
    for r in rest:
        new, sep = unseen(r)
        if new and used + _cost(sep.join(new) if sep else new[0]) <= allowance:
            take(r, new, sep)
    return out


# --- lean ------------------------------------------------------------------------------------

def _content_words(text: str) -> List[str]:
    return [w for w in _WORD.findall(text.lower()) if len(w) > 1 and w not in _STOP_WORDS]


_NEIGHBOUR_SQL = {
    # op and order are fixed here, not from input; values are ? parameters
    True: "SELECT text FROM memory_entries WHERE session_id = ? AND entry_id < ? "
          "AND state NOT IN ('archived', 'deleted') ORDER BY entry_id DESC LIMIT 1",
    False: "SELECT text FROM memory_entries WHERE session_id = ? AND entry_id > ? "
           "AND state NOT IN ('archived', 'deleted') ORDER BY entry_id ASC LIMIT 1",
}


def _session_neighbours(backend, entries) -> dict:
    """{entry_id: (turns of the memory saved just before, just after)} in the same session.

    One connection for all entries; each lookup uses the (session_id, entry_id) index.
    Only conversations continue across memories; entries without a session get nothing.
    """
    out = {}
    wanted = []
    for entry in entries:
        session = getattr(entry, "session_id", "") or ""
        try:
            wanted.append((entry.entry_id, session, int(entry.entry_id)))
        except (TypeError, ValueError):
            continue
    wanted = [w for w in wanted if w[1] not in ("", "default")]
    if not wanted or not hasattr(backend, "_get_conn"):
        return out
    with backend._get_conn() as conn:
        for key, session, entry_id in wanted:
            found = []
            for before in (True, False):
                row = conn.execute(_NEIGHBOUR_SQL[before], (session, entry_id)).fetchone()
                pieces, sep = split_pieces(row[0]) if row and row[0] else ([], "")
                found.append(pieces if sep == "\n" else [])
            out[key] = (found[0], found[1])
    return out


def lean_results(backend, query: str, pool: Sequence[SearchResult], top_k: int,
                 share: float = DEFAULT_LEAN_SHARE) -> List[SearchResult]:
    """Lean mode; see the module docstring. Never mutates the given results."""
    base = list(pool[:top_k])
    if not base:
        return []
    cap = sum(_cost(r.entry.text) for r in base)
    budget = min(1.0, max(0.1, share)) * cap

    # A stream per candidate: [previous memory's turns] + its own pieces + [next memory's turns]
    streams = []  # (result, pieces, own range, separator)
    split = [(r, *split_pieces(r.entry.text)) for r in pool]
    neighbours = _session_neighbours(backend, [r.entry for r, _, sep in split if sep == "\n"])
    for r, own, sep in split:
        before: List[str] = []
        after: List[str] = []
        if sep == "\n":
            mine = {_key(p) for p in own}
            before, after = neighbours.get(r.entry.entry_id, ([], []))
            before = [p for p in before if p.strip() and _key(p) not in mine]
            after = [p for p in after if p.strip() and _key(p) not in mine]
        streams.append((r, before + own, range(len(before), len(before) + len(own)), sep or "\n"))

    # Score each distinct piece (BM25 on content words) plus its memory's search score.
    best_at = {}  # piece key -> (has neighbours, stream, position)
    for si, (_, pieces, own, _) in enumerate(streams):
        for i in own:
            if not pieces[i].strip():
                continue
            inner = (i > 0) + (i + 1 < len(pieces))
            k = _key(pieces[i])
            if k not in best_at or inner > best_at[k][0]:
                best_at[k] = (inner, si, i)
    units = [(si, i) for _, si, i in best_at.values()]
    docs = [_content_words(streams[si][1][i]) for si, i in units]
    query_words = set(_content_words(query))
    n = len(docs)
    df = Counter(w for d in docs for w in set(d))
    avg = (sum(len(d) for d in docs) / n) if n else 1.0
    top_score = max((r.score for r in pool), default=1.0) or 1.0
    scored = []
    for (si, i), d in zip(units, docs):
        tf = Counter(d)
        s = sum(math.log((n - df[w] + 0.5) / (df[w] + 0.5) + 1) * tf[w] * 2.5
                / (tf[w] + 1.5 * (0.25 + 0.75 * len(d) / (avg or 1.0))) for w in query_words if w in tf)
        if s > 0:
            scored.append((s + streams[si][0].score / top_score, si, i))
    def fallback():  # standard results, cut to the budget (the first always fits the standard cost)
        out, spent = [], 0
        for r in compact_results(base, (), 0.0):
            if out and spent + _cost(r.entry.text) > budget:
                break
            out.append(r)
            spent += _cost(r.entry.text)
        return out

    if not scored:  # nothing to focus on, e.g. an empty query
        return fallback()
    scored.sort(key=lambda x: -x[0])

    shown = {}  # stream -> positions shown
    seen = set()
    used = 0

    def text_of(si, positions):
        return streams[si][3].join(streams[si][1][j] for j in sorted(positions))

    def add(si, positions, limit):
        nonlocal used
        pieces = streams[si][1]
        new = [j for j in positions if j not in shown.get(si, ()) and _key(pieces[j]) not in seen]
        if not new:
            return True
        before = _cost(text_of(si, shown[si])) if si in shown else 0
        cost = _cost(text_of(si, shown.get(si, set()) | set(new))) - before  # exact, separators included
        if used + cost > limit:
            return False
        shown.setdefault(si, set()).update(new)
        seen.update(_key(pieces[j]) for j in new)
        used += cost
        return True

    def window(si, i):
        pieces = streams[si][1]
        w = [j for j in (i - 1, i) if j >= 0]
        if i + 1 < len(pieces) and tokens(pieces[i + 1]) <= SHORT_TURN:
            w.append(i + 1)
        return w

    for _, si, i in scored:
        add(si, window(si, i), budget)
    if not shown:  # even the best window is over the lean budget: allow the standard cost for it
        _, si, i = scored[0]
        if not (add(si, window(si, i), cap) or add(si, [i], cap)):
            return fallback()

    order = sorted(shown, key=lambda si: -streams[si][0].score)
    return [_with_text(streams[si][0], streams[si][3].join(streams[si][1][j] for j in sorted(shown[si])),
                       source="excerpt")
            for si in order]


# --- entry point ----------------------------------------------------------------------------

def compact_search(backend, query: str, top_k: int = 5, spend: float = None, mode: Optional[str] = None,
                   **filters) -> List[SearchResult]:
    """``backend.search`` for a recall, in the requested (or configured) mode.

    SQLiteMemoryBackend.search ranks then cuts to top_k, so the first top_k of a larger
    search are exactly what ``search(top_k)`` returns; the rest are the candidates for
    extras. Other backends are not known to work that way and get plain search.
    """
    from cognicore.memory.sqlite_backend import SQLiteMemoryBackend

    if not compact_enabled() or not isinstance(backend, SQLiteMemoryBackend):
        return backend.search(query, top_k=top_k, **filters)
    mode = recall_mode(mode)
    spend = extra_spend() if spend is None else spend
    if mode == "rich":
        keep = top_k * RICH_FACTOR
        pool = backend.search(query, top_k=keep * 2, **filters)
        return compact_results(pool[:keep], pool[keep:], spend)
    pool = backend.search(query, top_k=top_k * POOL_FACTOR, **filters)
    if mode == "lean":
        return lean_results(backend, query, pool, top_k, lean_share())
    return compact_results(pool[:top_k], pool[top_k:], spend)

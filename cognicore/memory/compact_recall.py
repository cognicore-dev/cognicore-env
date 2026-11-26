"""Compact recall: the same results as search, each piece of text shown once, plus extras in the freed space.

``compact_results(base, rest)`` takes the ranked results a recall would show (``base``, the
top_k of ``search``) and the next-ranked ones (``rest``) and returns a list that:

* keeps everything ``base`` shows: a turn, paragraph or memory that already appeared in an
  earlier result is not repeated, and a result whose text has all appeared already is
  dropped; nothing else is cut, so no answer and no surrounding context is lost;
* never costs more tokens than ``base`` would have: the text saved by not repeating
  itself is the only room extras may use, and only a ``spend`` share of it;
* adds next-ranked memories whole (minus repeated pieces), only when they fit entirely.

When nothing repeats, the output is exactly ``base``.

Tokens are estimated as in the recall output: about 4 characters per token plus
RESULT_OVERHEAD per result for its score, date and id. Pieces are compared ignoring case
and runs of whitespace. Settings (environment):

    COGNICORE_COMPACT_RECALL      "0" turns compact recall off (plain search results)
    COGNICORE_RECALL_EXTRA_SPEND  share of the saved tokens extras may use, 0..1 (default 0.5)
"""

import copy
import os
import re
from typing import List, Sequence, Tuple

from cognicore.memory.base import SearchResult

RESULT_OVERHEAD = 12
DEFAULT_EXTRA_SPEND = 0.5
# Candidates fetched for extras: top_k x this.
POOL_FACTOR = 4

_SPEAKER = re.compile(r"^[A-Za-z][\w .'-]{0,30}: ")
_PARAGRAPH = re.compile(r"\n\s*\n")


def compact_enabled() -> bool:
    return os.environ.get("COGNICORE_COMPACT_RECALL", "1").strip() != "0"


def extra_spend() -> float:
    try:
        value = float(os.environ.get("COGNICORE_RECALL_EXTRA_SPEND", DEFAULT_EXTRA_SPEND))
    except ValueError:
        return DEFAULT_EXTRA_SPEND
    return min(1.0, max(0.0, value))


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


def _with_text(result: SearchResult, text: str) -> SearchResult:
    if text == result.entry.text:
        return result
    entry = copy.copy(result.entry)
    entry.text = text
    return SearchResult(entry=entry, score=result.score, source=result.source)


def compact_results(base: Sequence[SearchResult], rest: Sequence[SearchResult] = (),
                    spend: float = DEFAULT_EXTRA_SPEND) -> List[SearchResult]:
    """See the module docstring. Never mutates the given results."""
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


def compact_search(backend, query: str, top_k: int = 5, spend: float = None, **filters) -> List[SearchResult]:
    """``backend.search`` for a recall: compact when enabled, plain otherwise.

    SQLiteMemoryBackend.search ranks then cuts to top_k, so the first top_k of a larger
    search are exactly what ``search(top_k)`` returns; the rest are the candidates for
    extras. Other backends are not known to work that way and get plain search.
    """
    from cognicore.memory.sqlite_backend import SQLiteMemoryBackend

    if not compact_enabled() or not isinstance(backend, SQLiteMemoryBackend):
        return backend.search(query, top_k=top_k, **filters)
    pool = backend.search(query, top_k=top_k * POOL_FACTOR, **filters)
    return compact_results(pool[:top_k], pool[top_k:], extra_spend() if spend is None else spend)

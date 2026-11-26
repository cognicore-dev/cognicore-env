"""Compact recall: never fewer answers, never less context, never more tokens than search.

The guarantees pinned here:
  1. every piece of text the plain search shows is still shown (once);
  2. the output never costs more tokens than the plain search output;
  3. with nothing repeated and nothing extra fitting, the output is the plain search output;
  4. extras are whole memories (minus repeated pieces) and only use freed space;
  5. COGNICORE_COMPACT_RECALL=0 gives plain search, and other backends always get plain search.
"""

import random
from types import SimpleNamespace

import pytest

from cognicore.memory import compact_recall as cr
from cognicore.memory.base import MemoryEntry, SearchResult
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend


def _r(text, score=1.0, entry_id="1"):
    return SearchResult(entry=MemoryEntry(text=text, entry_id=entry_id), score=score, source="sqlite")


def _out_cost(results):
    return sum(cr._cost(r.entry.text) for r in results)


def _pieces_shown(results):
    keys = set()
    for r in results:
        keys.update(cr._key(p) for p in cr.split_pieces(r.entry.text)[0] if p.strip())
    return keys


# --- splitting --------------------------------------------------------------------------

def test_split_conversation_into_turns_and_back():
    text = "user: hi there\nassistant: line one\nline two\nuser: thanks"
    pieces, sep = cr.split_pieces(text)
    assert pieces == ["user: hi there", "assistant: line one\nline two", "user: thanks"]
    assert sep.join(pieces) == text


def test_split_paragraphs_and_short_facts():
    assert cr.split_pieces("first para\n\nsecond para") == (["first para", "second para"], "\n\n")
    assert cr.split_pieces("The payments API runs on port 9000") == (["The payments API runs on port 9000"], "")
    assert cr.split_pieces("Note: one line\nanother line") == (["Note: one line\nanother line"], "")


# --- guarantees on compact_results -------------------------------------------------------

def test_no_repeats_returns_exactly_the_search_results():
    base = [_r("alpha fact", 0.9, "1"), _r("beta fact", 0.5, "2")]
    out = cr.compact_results(base, rest=[], spend=1.0)
    assert out == base


def test_repeated_turns_shown_once_and_duplicate_memory_dropped():
    a = _r("user: q1\nassistant: a1\nuser: q2", 0.9, "1")
    b = _r("assistant: a1\nuser: q2\nassistant: a2", 0.8, "2")  # overlaps a by two turns
    c = _r("User:  Q1\nassistant: A1\nuser: q2", 0.7, "3")      # same as a, different case/spacing
    out = cr.compact_results([a, b, c], spend=0.0)
    assert [r.entry.entry_id for r in out] == ["1", "2"]
    assert out[1].entry.text == "assistant: a2"
    assert _pieces_shown(out) >= _pieces_shown([a, b, c])
    assert b.entry.text == "assistant: a1\nuser: q2\nassistant: a2"  # inputs are not mutated


def test_extras_only_from_freed_space_and_whole():
    a = _r("user: " + "x" * 400 + "\nassistant: " + "y" * 400, 0.9, "1")
    dup = _r("user: " + "x" * 400 + "\nassistant: " + "y" * 400, 0.8, "2")
    extra_small = _r("a short next fact", 0.5, "3")
    extra_big = _r("z" * 5000, 0.4, "4")
    out = cr.compact_results([a, dup], rest=[extra_small, extra_big], spend=1.0)
    assert [r.entry.entry_id for r in out] == ["1", "3"]  # the big one does not fit, nothing is cut
    assert out[1].entry.text == "a short next fact"
    assert _out_cost(out) <= _out_cost([a, dup])
    # spend 0: freed space is all kept as savings
    assert [r.entry.entry_id for r in cr.compact_results([a, dup], [extra_small], spend=0.0)] == ["1"]


@pytest.mark.parametrize("seed", range(300))
def test_random_never_worse(seed):
    rng = random.Random(seed)
    turns = [f"{rng.choice(['user', 'assistant'])}: {'w' * rng.randint(1, 300)} {i}" for i in range(40)]

    def memory(i):
        start = rng.randint(0, 35)
        if rng.random() < 0.3:
            return _r(f"fact {rng.randint(0, 8)} " + "v" * rng.randint(0, 200), entry_id=str(i))
        return _r("\n".join(turns[start:start + rng.randint(1, 5)]), entry_id=str(i))

    ranked = [memory(i) for i in range(rng.randint(1, 20))]
    for i, r in enumerate(ranked):
        r.score = 1.0 - i / 100
    k = rng.randint(1, 6)
    base, rest = ranked[:k], ranked[k:]
    out = cr.compact_results(base, rest, spend=rng.choice([0.0, 0.5, 1.0]))
    assert _out_cost(out) <= _out_cost(base)               # never more tokens
    assert _pieces_shown(out) >= _pieces_shown(base)        # nothing the search showed is lost
    assert [r.score for r in out] == sorted((r.score for r in out), reverse=True)
    if len({cr._key(p) for r in base for p in cr.split_pieces(r.entry.text)[0]}) == sum(
            len(cr.split_pieces(r.entry.text)[0]) for r in base) and not rest:
        assert out == base                                 # nothing repeated, nothing extra


# --- compact_search on a real database ------------------------------------------------------

@pytest.fixture
def backend(tmp_path):
    b = SQLiteMemoryBackend(str(tmp_path / "m.db"))
    convo = [f"user: question {i} about billing retries" if i % 2 == 0 else f"assistant: answer {i} about billing"
             for i in range(12)]
    entries = ["\n".join(convo[i:i + 5]) for i in range(0, 8, 3)]  # overlapping session chunks
    entries += ["The billing service retries failed charges three times", "Invoices go out on the first of the month"]
    for i, t in enumerate(entries):
        b.store(MemoryEntry(text=t, category="general", timestamp=1000.0 + i))
    return b


def test_compact_search_keeps_search_results_and_never_costs_more(backend, monkeypatch):
    monkeypatch.delenv("COGNICORE_COMPACT_RECALL", raising=False)
    plain = backend.search("billing retries", top_k=3)
    out = cr.compact_search(backend, "billing retries", top_k=3, spend=1.0)
    assert _pieces_shown(out) >= _pieces_shown(plain)
    assert _out_cost(out) <= _out_cost(plain)
    assert [r.entry.entry_id for r in out[:1]] == [plain[0].entry.entry_id]


def test_first_k_of_larger_search_equal_search_k(backend):
    for q in ("billing retries", "invoices", "question answer", ""):
        for k in (1, 2, 3, 5):
            assert [r.entry.entry_id for r in backend.search(q, top_k=k * cr.POOL_FACTOR)[:k]] == \
                   [r.entry.entry_id for r in backend.search(q, top_k=k)]


def test_switch_off_gives_plain_search(backend, monkeypatch):
    monkeypatch.setenv("COGNICORE_COMPACT_RECALL", "0")
    plain = backend.search("billing retries", top_k=3)
    assert [(r.entry.entry_id, r.entry.text) for r in cr.compact_search(backend, "billing retries", top_k=3)] == \
           [(r.entry.entry_id, r.entry.text) for r in plain]


def test_other_backends_get_plain_search():
    calls = []
    other = SimpleNamespace(search=lambda q, top_k, **kw: calls.append(top_k) or [_r("x")])
    assert [r.entry.text for r in cr.compact_search(other, "x", top_k=4)] == ["x"]
    assert calls == [4]


@pytest.mark.parametrize("value,expected", [(None, 0.5), ("1", 1.0), ("0", 0.0), ("2", 1.0), ("-1", 0.0), ("x", 0.5)])
def test_extra_spend_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("COGNICORE_RECALL_EXTRA_SPEND", raising=False)
    else:
        monkeypatch.setenv("COGNICORE_RECALL_EXTRA_SPEND", value)
    assert cr.extra_spend() == expected

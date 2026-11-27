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


# --- modes ----------------------------------------------------------------------------------

@pytest.mark.parametrize("requested,env,expected", [
    (None, None, "standard"), ("lean", None, "lean"), ("RICH", None, "rich"), ("bogus", None, "standard"),
    (None, "lean", "lean"), ("rich", "lean", "rich"), (None, "bogus", "standard"),
])
def test_recall_mode_resolution(monkeypatch, requested, env, expected):
    if env is None:
        monkeypatch.delenv("COGNICORE_RECALL_MODE", raising=False)
    else:
        monkeypatch.setenv("COGNICORE_RECALL_MODE", env)
    assert cr.recall_mode(requested) == expected


def _conversation_db(tmp_path, n_turns=40, seed=0):
    """A saved conversation as overlapping 5-turn memories of one session, plus loose facts."""
    rng = random.Random(seed)
    topics = ["billing retries", "deploy script", "oauth tokens", "invoice dates", "redis cache", "export csv"]
    turns = []
    for i in range(n_turns):
        role = "user" if i % 2 == 0 else "assistant"
        turns.append(f"{role}: turn {i} on {rng.choice(topics)} " + "detail " * rng.randint(1, 40))
    b = SQLiteMemoryBackend(str(tmp_path / f"c{seed}.db"))
    for i in range(0, n_turns - 4, 3):
        b.store(MemoryEntry(text="\n".join(turns[i:i + 5]), category="history", session_id="s-1",
                            timestamp=1000.0 + i))
    for j, t in enumerate(topics):
        b.store(MemoryEntry(text=f"Fact: the {t} setting was changed", timestamp=2000.0 + j))
    return b, turns, topics


def _all_source_pieces(backend):
    with backend._get_conn() as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM memory_entries").fetchall()]
    return {cr._key(p) for t in texts for p in cr.split_pieces(t)[0]}


@pytest.mark.parametrize("seed", range(40))
def test_lean_never_costs_more_and_never_garbles(tmp_path, monkeypatch, seed):
    monkeypatch.delenv("COGNICORE_COMPACT_RECALL", raising=False)
    b, turns, topics = _conversation_db(tmp_path, seed=seed)
    source = _all_source_pieces(b)
    rng = random.Random(seed)
    for _ in range(5):
        q = rng.choice(topics) + rng.choice(["", " detail", " what changed"])
        k = rng.randint(1, 5)
        plain = b.search(q, top_k=k)
        lean = cr.compact_search(b, q, top_k=k, mode="lean")
        assert _out_cost(lean) <= _out_cost(plain)               # never more than standard's base
        assert bool(lean) == bool(plain)                         # never empty when search found something
        for r in lean:                                           # every piece is verbatim stored text
            assert all(cr._key(p) in source for p in cr.split_pieces(r.entry.text)[0])
        assert [r.score for r in lean] == sorted((r.score for r in lean), reverse=True)


def test_lean_shows_matching_turn_with_turn_before_across_memories(tmp_path):
    b = SQLiteMemoryBackend(str(tmp_path / "m.db"))
    convo = ["user: how do refunds work", "assistant: refunds go back within five days",
             "user: thanks", "assistant: anything else", "user: no"]
    pad = [f"{'user' if i % 2 == 0 else 'assistant'}: unrelated chatter number {i} " + "x " * 60 for i in range(10)]
    turns = pad[:4] + convo + pad[4:]
    # memory 1 ends just before the question; memory 2 starts with the question
    b.store(MemoryEntry(text="\n".join(turns[0:4]), session_id="s-9", timestamp=1.0))
    b.store(MemoryEntry(text="\n".join(turns[4:9]), session_id="s-9", timestamp=2.0))
    b.store(MemoryEntry(text="\n".join(turns[9:14]), session_id="s-9", timestamp=3.0))
    out = cr.compact_search(b, "refunds five days", top_k=3, mode="lean")
    text = "\n".join(r.entry.text for r in out)
    assert "assistant: refunds go back within five days" in text
    assert "user: how do refunds work" in text                   # the turn before comes along
    assert all(r.source == "excerpt" for r in out)
    assert _out_cost(out) < _out_cost(b.search("refunds five days", top_k=3))


def test_lean_keeps_short_facts_whole(tmp_path):
    b = SQLiteMemoryBackend(str(tmp_path / "m.db"))
    b.store(MemoryEntry(text="The payments API runs on port 9000"))
    out = cr.compact_search(b, "payments port", top_k=5, mode="lean")
    assert [r.entry.text for r in out] == ["The payments API runs on port 9000"]


@pytest.mark.parametrize("seed", range(40))
def test_rich_keeps_search_results_within_twice_k(tmp_path, monkeypatch, seed):
    monkeypatch.delenv("COGNICORE_COMPACT_RECALL", raising=False)
    b, turns, topics = _conversation_db(tmp_path, seed=seed)
    rng = random.Random(seed)
    for _ in range(5):
        q = rng.choice(topics)
        k = rng.randint(1, 4)
        rich = cr.compact_search(b, q, top_k=k, mode="rich")
        assert _pieces_shown(rich) >= _pieces_shown(b.search(q, top_k=k))          # all of search's top k
        assert _pieces_shown(rich) >= _pieces_shown(cr.compact_search(b, q, top_k=k))  # all standard's base
        assert _out_cost(rich) <= _out_cost(b.search(q, top_k=k * cr.RICH_FACTOR))


def test_mode_setting_from_environment(tmp_path, monkeypatch):
    b, _, _ = _conversation_db(tmp_path)
    monkeypatch.setenv("COGNICORE_RECALL_MODE", "lean")
    assert all(r.source == "excerpt" for r in cr.compact_search(b, "billing retries", top_k=3))
    monkeypatch.setenv("COGNICORE_COMPACT_RECALL", "0")
    plain = b.search("billing retries", top_k=3)
    assert [r.entry.text for r in cr.compact_search(b, "billing retries", top_k=3)] == [r.entry.text for r in plain]


def test_hosted_recall_passes_mode_and_rejects_unknown(tmp_path, monkeypatch):
    from cognicore.connectors import remote

    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("COGNICORE_DB_PATH", raising=False)
    seen = []
    monkeypatch.setattr(remote, "compact_search", lambda backend, q, **kw: seen.append(kw.get("mode")) or [])
    assert remote.cognicore_recall("x", None, mode="bogus").startswith("Error: mode must be")
    remote.cognicore_recall("x", None, mode="lean")
    remote.cognicore_recall("x", None)
    assert seen == ["lean", None]

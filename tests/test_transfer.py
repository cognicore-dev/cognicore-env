"""Tests for session save/resume/compress: the cross-agent memory transfer path."""
import json

import pytest

from cognicore.memory import SQLiteMemoryBackend
from cognicore.memory.context_preservation import compress_context, save_session


@pytest.fixture
def backend(tmp_path):
    return SQLiteMemoryBackend(str(tmp_path / "mem.db"))


CONV = [
    {"role": "user", "content": "Our goal is to fix intermittent 401s on /api/v2/orders. Ticket is AUTH-1423."},
    {"role": "assistant", "content": "We decided to switch to PyJWT 2.8.0 because python-jose is unmaintained."},
    {"role": "user", "content": "TODO: add a regression test in tests/test_auth.py."},
]


def test_two_saves_in_the_same_second_keep_separate_sessions(backend):
    a = json.loads(save_session(backend, CONV, session_name="A"))
    b = json.loads(save_session(backend, CONV[:1], session_name="B"))
    assert a["session_id"] != b["session_id"]
    with backend._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_compress_keep_last_n_zero_compresses_everything(backend):
    data = json.loads(compress_context(backend, CONV, keep_last_n=0))
    assert data["messages_compressed"] == len(CONV)
    assert "AUTH-1423" in data["summary"]


def test_compress_has_no_hardcoded_project_goal(backend):
    conv = [{"role": "user", "content": "Brand color is #111111 and pricing is $12/month."}]
    data = json.loads(compress_context(backend, conv, keep_last_n=0))
    assert "CogniCore MCP server" not in data["summary"]


class _CountingProvider:
    """Minimal embedding provider that records how it was called."""

    def __init__(self):
        self.single, self.batches = 0, []

    def embed(self, text):
        self.single += 1
        return [float(len(text)), 1.0]

    def embed_batch(self, texts):
        self.batches.append(len(texts))
        return [[float(len(t)), 1.0] for t in texts]

    @property
    def dimension(self):
        return 2


def test_store_many_matches_store_and_batches_embeddings(tmp_path):
    from cognicore.memory.base import MemoryEntry

    provider = _CountingProvider()
    b = SQLiteMemoryBackend(str(tmp_path / "m.db"), provider=provider)
    ids = b.store_many([MemoryEntry(text=f"fact number {i}", category="general") for i in range(5)])
    assert len(ids) == 5 and len(set(ids)) == 5
    assert provider.batches == [5] and provider.single == 0
    with b._get_conn() as conn:
        rows = conn.execute("SELECT text, embedding_json FROM memory_entries ORDER BY entry_id").fetchall()
    assert [r["text"] for r in rows] == [f"fact number {i}" for i in range(5)]
    assert all(r["embedding_json"] for r in rows)
    assert b.store_many([]) == []


def test_compress_reports_expansion_honestly(backend):
    conv = [{"role": "user", "content": "Launch is on November 15."}]
    data = json.loads(compress_context(backend, conv, keep_last_n=0))
    assert data["expanded"] is (data["tokens_after"] > data["tokens_before"])
    ratio = float(data["compression_ratio"].rstrip("x"))
    assert ratio == pytest.approx(data["tokens_before"] / max(1, data["tokens_after"]), abs=0.05)


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

from cognicore.memory.redact import redact_secrets  # noqa: E402


@pytest.mark.parametrize("text", [
    "key: sk-test-FAKE0000000000000000000000",
    "password=hunter2-Staging!",
    "the password is Tr0ub4dor&3",
    "postgres://admin:s3cretPass@db.internal:5432/app",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
])
def test_redact_secrets_catches_common_credentials(text):
    out, n = redact_secrets(text)
    assert n >= 1 and "[REDACTED:" in out


def test_redact_secrets_keeps_exact_values():
    text = ("Committed as 9f3c2ab (3f786850e387550fdab836ed7e6dc881de23001b). PyJWT 2.8.0, leeway=30, "
            "DATABASE_URL_V2, migrations/0042_rename_user_id.sql, token TTL to 24h, #111111, AUTH-1423, "
            "https://cognicore.dev/docs, src/components/very/long/path/SomeComponentFile123.tsx")
    assert redact_secrets(text) == (text, 0)


# ---------------------------------------------------------------------------
# Handoff format
# ---------------------------------------------------------------------------

from cognicore.memory.handoff import (  # noqa: E402
    SCHEMA_VERSION, build_handoff, extract_exact_values, is_chatter, validate_handoff,
)

RICH = [
    {"role": "user", "content": "Hey! Can you help me with something?"},
    {"role": "user", "content": "Our goal is to fix intermittent 401s on /api/v2/orders. Ticket is AUTH-1423. "
                                "We run Python 3.11.4."},
    {"role": "assistant", "content": "The root cause is clock skew between pods."},
    {"role": "user", "content": "We tried increasing the token TTL to 24h but it did not help."},
    {"role": "assistant", "content": "We decided to switch to PyJWT 2.8.0 because python-jose is unmaintained."},
    {"role": "user", "content": "Committed as 9f3c2ab. Key is sk-test-FAKE0000000000000000000000"},
    {"role": "assistant", "content": "TODO: add a regression test in tests/test_auth.py.\n```python\nleeway = 30\n```"},
    {"role": "user", "content": "Sounds good."},
]


def test_build_handoff_sections_and_schema():
    h = build_handoff(RICH, "sess_1", "jwt", 1_700_000_000.0, agent="claude-code", client="vscode",
                      environment={"os": "macOS", "python_version": "3.11.4", "junk": "x"})
    assert validate_handoff(h) == []
    assert h["schema"] == SCHEMA_VERSION
    assert "/api/v2/orders" in h["goal"]
    assert "clock skew" in h["current_state"] and "9f3c2ab" in h["current_state"]
    made = [d for d in h["decisions"] if d["status"] == "made"]
    assert made[0]["what"].endswith("PyJWT 2.8.0") and made[0]["why"] == "python-jose is unmaintained."
    assert any(d["status"] == "rejected" and "24h" in d["what"] for d in h["decisions"])
    assert h["open_tasks"] == [{"task": "TODO: add a regression test in tests/test_auth.py.", "status": "open"}]
    assert {"/api/v2/orders", "tests/test_auth.py"} <= set(h["files"])
    assert h["code"][0]["code"] == "leeway = 30"
    assert h["provenance"] == {
        "session_id": "sess_1", "session_name": "jwt", "agent": "claude-code", "client": "vscode",
        "created_at": "2023-11-14T22:13:20Z", "message_count": len(RICH), "migrated": False,
    }
    assert h["environment"] == {"os": "macOS", "python_version": "3.11.4"}
    dump = json.dumps(h)
    assert "sk-test-FAKE" not in dump and h["redactions"] == 1
    assert "Sounds good" not in dump and "Can you help me" not in dump


def test_explicit_goal_and_state_win():
    h = build_handoff(RICH, "s", "n", 0.0, goal="Ship the JWT fix", current_state="PR open, CI green")
    assert h["goal"] == "Ship the JWT fix" and h["current_state"] == "PR open, CI green"
    assert any("clock skew" in f for f in h["key_facts"])  # extracted state is kept as a fact


def test_handoff_dedupes_repeated_and_contained_statements():
    conv = [{"role": "user", "content": "Brand color is #111111."},
            {"role": "user", "content": "brand color is #111111"},
            {"role": "user", "content": "Brand color is #111111. Pricing is $12/month."},
            {"role": "user", "content": "Brand color is #111111 for the whole site."}]
    h = build_handoff(conv, "s", "n", 0.0)
    texts = [h["goal"], *h["key_facts"]]
    assert sum("#111111" in t for t in texts) == 1


def test_exact_values_are_verbatim():
    vals = {v["value"] for v in extract_exact_values(
        "Use PyJWT 2.8.0 at auth/tokens.py, commit 9f3c2ab, AUTH-1423, $12/month, 1,240 signups, "
        "DATABASE_URL_V2, error 42P01, cognicore.dev, Nov 8, leeway=30")}
    assert {"PyJWT 2.8.0", "auth/tokens.py", "9f3c2ab", "AUTH-1423", "$12/month", "1,240 signups",
            "DATABASE_URL_V2", "42P01", "cognicore.dev", "Nov 8", "leeway=30"} <= vals


@pytest.mark.parametrize("sentence,chatter", [
    ("Can you help me with something?", True),
    ("Sounds good.", True),
    ("Ok, let me think about that for a second and get back to you shortly.", True),
    ("Right, TTL does not affect the nbf check.", False),
    ("Should we use Redis 7.2 for the cache?", False),
    ("Target audience is indie developers.", False),
])
def test_is_chatter(sentence, chatter):
    assert is_chatter(sentence) is chatter


def test_validate_handoff_reports_problems():
    assert validate_handoff([]) == ["handoff must be an object"]
    bad = build_handoff(RICH, "s", "n", 0.0)
    bad["schema"] = "v0"
    bad["decisions"] = [{"what": "x", "status": "maybe"}]
    errors = validate_handoff(bad)
    assert any("schema" in e for e in errors) and any("decisions" in e for e in errors)


def test_schema_file_matches_builder():
    """docs/handoff-schema.json accepts what build_handoff produces."""
    from pathlib import Path

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parent.parent / "docs" / "handoff-schema.json").read_text())
    jsonschema.validate(build_handoff(RICH, "s", "n", 0.0, environment={"os": "Linux"}), schema)


# ---------------------------------------------------------------------------
# save_session / resume_session
# ---------------------------------------------------------------------------

from cognicore.memory.context_preservation import TokenBudget, resume_session  # noqa: E402


def _rows(b):
    with b._get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM memory_entries").fetchall()]


def test_save_stores_handoff_provenance_and_no_secret(backend):
    res = json.loads(save_session(backend, RICH, session_name="jwt", agent="cursor", client="cursor-ide"))
    assert res["schema"] == SCHEMA_VERSION and res["redactions"] == 1 and not res["deduplicated"]
    with backend._get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (res["session_id"],)).fetchone()
    handoff = json.loads(row["handoff_json"])
    assert validate_handoff(handoff) == []
    assert row["agent"] == "cursor" and row["client"] == "cursor-ide"
    rows = _rows(backend)
    assert all(r["source_agent"] == "cursor" for r in rows)
    assert not any("sk-test-FAKE" in r["text"] for r in rows)
    norm = [r["text"].lower() for r in rows]
    assert len(norm) == len(set(norm))


def test_resave_is_idempotent(backend):
    first = json.loads(save_session(backend, RICH))
    n = len(_rows(backend))
    again = json.loads(save_session(backend, RICH))
    assert again["deduplicated"] is True and again["session_id"] == first["session_id"]
    assert len(_rows(backend)) == n


def test_growing_conversation_links_existing_rows(backend):
    save_session(backend, RICH[:4])
    n = len(_rows(backend))
    res = json.loads(save_session(backend, RICH))
    assert res["already_in_memory"] >= 1
    texts = [r["text"] for r in _rows(backend)]
    assert len(texts) == len(set(texts)) and len(texts) > n


def test_resume_returns_handoff_sections_in_priority_order(backend):
    save_session(backend, RICH, session_name="jwt", agent="claude-code")
    out = resume_session(backend)
    assert out.startswith("=== COGNICORE CONTEXT RESUME ===") and out.rstrip().endswith("tokens ===")
    order = [out.index(h) for h in ("Goal:", "Current state:", "Open tasks:", "Decisions:", "Files/resources:")]
    assert order == sorted(order)
    for value in ("AUTH-1423", "3.11.4", "PyJWT 2.8.0", "9f3c2ab", "24h", "tests/test_auth.py", "leeway = 30"):
        assert value in out
    assert "by claude-code" in out and "sk-test-FAKE" not in out


LONG = RICH + [{"role": "user", "content": f"Fact {i}: service-{i} listens on port {8000 + i}."} for i in range(40)]


@pytest.mark.parametrize("budget", [120, 200, 400])
def test_resume_respects_token_budget_and_keeps_goal_first(backend, budget):
    save_session(backend, LONG, session_name="big")
    out = resume_session(backend, max_tokens=budget)
    assert TokenBudget.estimate_tokens(out) <= budget
    assert "Goal:" in out and "/api/v2/orders" in out
    assert "More available" in out


def test_resume_budget_below_minimum_is_raised(backend):
    save_session(backend, LONG, session_name="big")
    out = resume_session(backend, max_tokens=10)
    assert "Goal:" in out and TokenBudget.estimate_tokens(out) <= 120
    doc = json.loads(resume_session(backend, format="json", max_tokens=10))
    assert doc["max_tokens_applied"] == 200 and doc["goal"]


def test_resume_bigger_budget_returns_more(backend):
    save_session(backend, LONG, session_name="big")
    sizes = [TokenBudget.estimate_tokens(resume_session(backend, max_tokens=b)) for b in (150, 300, 600)]
    assert sizes == sorted(sizes) and sizes[0] < sizes[-1]


def test_resume_json_format_and_on_demand_section(backend):
    sid = json.loads(save_session(backend, LONG))["session_id"]
    doc = json.loads(resume_session(backend, format="json", max_tokens=250))
    assert doc["schema"] == SCHEMA_VERSION and doc["provenance"]["session_id"] == sid
    assert doc["goal"] and "omitted" in doc
    assert doc["tokens"] <= 250
    omitted_facts = doc["omitted"].get("key_facts", 0)
    full = json.loads(resume_session(backend, session_id=sid, section="key_facts", format="json"))
    assert len(full["key_facts"]) >= omitted_facts + len(doc.get("key_facts", []))
    facts = json.loads(resume_session(backend, session_id=sid, section="exact_values", format="json"))
    assert "AUTH-1423" in {v["value"] for v in facts["exact_values"]}
    assert "error" in resume_session(backend, section="nope").lower()
    assert "no saved session" in resume_session(backend, session_id="sess_missing").lower()


def test_resume_picks_session_by_query_and_lists_others(backend):
    save_session(backend, RICH, session_name="jwt")
    save_session(backend, [{"role": "user", "content": "Our product launch is on November 15. Brand color is #111111."}],
                 session_name="launch")
    latest = resume_session(backend)
    assert "November 15" in latest.split("Other recent sessions:")[0]
    by_query = resume_session(backend, query="JWT 401 auth")
    assert "AUTH-1423" in by_query.split("Other recent sessions:")[0]
    assert "launch" in by_query.split("Other recent sessions:")[1]
    only_one = resume_session(backend, last_n_sessions=1)
    assert "Other recent sessions:" not in only_one


def test_resume_can_skip_action_items(backend):
    save_session(backend, RICH)
    assert "Open tasks:" not in resume_session(backend, include_action_items=False)


def test_resume_warns_on_environment_change(backend):
    env = {"os": "macOS", "python_version": "3.11.4", "framework": "FastAPI", "framework_version": "0.110.0",
           "dependencies": {"pyjwt": "2.8.0"}}
    save_session(backend, RICH, environment=json.dumps(env))
    same = resume_session(backend, environment=env)
    assert "Environment warnings" not in same
    changed = resume_session(backend, environment={**env, "os": "Windows", "dependencies": {"pyjwt": "1.7.1"}})
    assert "Environment warnings" in changed and "OS mismatch" in changed and "BLOCKER" in changed


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

def _make_pre_v1_db(path):
    """A database as written by the pre-handoff save_session."""
    import sqlite3

    from cognicore.memory.base import MemoryEntry

    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE sessions (session_id TEXT PRIMARY KEY, name TEXT, timestamp REAL,
                    message_count INTEGER, total_tokens_estimated INTEGER, summary_memory_id INTEGER)""")
    conn.commit()
    conn.close()
    b = SQLiteMemoryBackend(str(path))
    texts = [
        ("Our goal is to fix intermittent 401s on /api/v2/orders", "general"),
        ("Ticket is AUTH-1423", "general"),
        ("We decided to switch to PyJWT 2.8.0 because python-jose is unmaintained.", "decisions"),
        ("TODO: add a regression test in tests/test_auth.py.", "action_items"),
        ("Session 'old' (01 Jan 2026):\n- Decided: We decided to switch to PyJWT 2.8.0", "session_summary"),
    ]
    ids = [b.store(MemoryEntry(text=t, category=c, session_id="sess_20260101_120000", timestamp=1_767_268_800.0))
           for t, c in texts]
    with b._get_conn() as conn:
        conn.execute("INSERT INTO sessions (session_id, name, timestamp, message_count, total_tokens_estimated,"
                     " summary_memory_id) VALUES (?, ?, ?, ?, ?, ?)",
                     ("sess_20260101_120000", "old", 1_767_268_800.0, 6, 120, int(ids[-1])))
    return b


def test_pre_v1_database_is_migrated_and_old_session_resumes(tmp_path):
    b = _make_pre_v1_db(tmp_path / "old.db")
    with b._get_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert {"handoff_json", "conversation_hash", "agent", "client"} <= cols

    out = resume_session(b)
    assert "migrated from pre-v1 session" in out
    for value in ("/api/v2/orders", "AUTH-1423", "PyJWT 2.8.0", "tests/test_auth.py"):
        assert value in out
    doc = json.loads(resume_session(b, format="json"))
    assert doc["provenance"]["migrated"] is True
    assert doc["open_tasks"][0]["task"].startswith("TODO")
    with b._get_conn() as conn:  # resume is read-only: nothing written back
        assert conn.execute("SELECT handoff_json FROM sessions").fetchone()[0] is None


def test_resume_without_any_sessions_falls_back_to_memories(backend):
    from cognicore.memory.base import MemoryEntry

    backend.store(MemoryEntry(text="Deploy target is Railway", category="general"))
    out = resume_session(backend)
    assert "Deploy target is Railway" in out and out.rstrip().endswith("tokens ===")


def test_compress_redacts_secrets(backend):
    conv = [{"role": "user", "content": "Deploy key is sk-test-FAKE0000000000000000000000 for 3 envs."}]
    data = json.loads(compress_context(backend, conv, keep_last_n=0))
    assert "sk-test-FAKE" not in data["summary"] and data["redactions"] == 1

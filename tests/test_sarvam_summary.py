"""Hosted AI summaries of synced sessions with Sarvam (no network: the API call is faked)."""
import json
import time

import pytest

from cognicore.memory import SQLiteMemoryBackend
from cognicore.memory import sarvam_summary as ss


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")
    monkeypatch.setenv("COGNICORE_SUMMARY_MIN_TOKENS", "100")
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)


def _handoff(n_tasks=30):
    return {
        "schema": "cognicore.handoff/v1",
        "provenance": {"session_id": "sess_x", "session_name": "billing-app", "agent": "claude-code",
                       "client": "claude-code-hooks", "created_at": "2026-11-15T10:00:00Z",
                       "message_count": 40, "migrated": False, "project": "/work/billing"},
        "goal": "Fix BILL-311 rounding in format_eur",
        "current_state": "Decimal rounding merged in #42; docs pending.",
        "completed": ["Merged #42"],
        "decisions": [{"what": "Use ROUND_HALF_UP", "why": "finance requires commercial rounding",
                       "status": "made"}],
        "open_tasks": [{"task": f"Right, you don't need to do step {i} of the billing rollout again",
                        "status": "open"} for i in range(n_tasks)]
        + [{"task": "Update docs/billing.md for BILL-311", "status": "open"}],
        "key_facts": ["Reviewer is Anika"], "exact_values": [{"value": "BILL-311", "kind": "ticket"}],
        "files": ["billing/tax.py"], "code": [], "environment": {}, "redactions": 0,
    }


def _reply(summary, finish="stop"):
    content = "<think>the user wants a summary</think>\n```json\n" + json.dumps(summary) + "\n```"
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 300}}


GOOD = {"goal": "Fix BILL-311 rounding in format_eur", "current_state": "Rounding merged in #42; docs left.",
        "decisions": [{"what": "Use ROUND_HALF_UP", "why": "finance requires commercial rounding"},
                      {"what": "Rejected: banker's rounding", "why": ""}],
        "open_tasks": ["Update docs/billing.md for BILL-311", "Deploy v9.9.9 to prod"],  # v9.9.9 is invented
        "key_facts": ["Reviewer is Anika", "BILL-311", "Use ROUND_HALF_UP", "billing/tax.py"],
        "files": ["docs/billing.md"]}


def test_summary_keeps_grounded_items_and_drops_invented_values():
    sent = {}

    def post(url, body, key, timeout):
        sent.update(url=url, body=body, key=key)
        return _reply(GOOD)

    summary, info = ss.summarize_handoff(_handoff(), api_key="k-test", post=post)
    assert sent["url"] == ss.SARVAM_URL and sent["body"]["model"] == "sarvam-105b"
    assert sent["body"]["max_tokens"] >= 4000  # reasoning model
    assert summary["open_tasks"] == ["Update docs/billing.md for BILL-311"]
    assert info["dropped_ungrounded"] == 1 and info["provider"] == "sarvam"

    doc = ss.apply_summary(_handoff(), summary, info)
    assert doc["exact_values"] == _handoff()["exact_values"] and doc["completed"] == ["Merged #42"]
    assert doc["files"] == ["billing/tax.py", "docs/billing.md"]
    assert doc["decisions"][1] == {"what": "banker's rounding", "why": "", "status": "rejected"}
    assert doc["compression"]["model"] == "sarvam-105b"
    assert doc["key_facts"] == ["Reviewer is Anika"]  # repeats of the goal, a decision and a file are dropped


def test_secrets_are_redacted_before_sending():
    doc = _handoff()
    fake_key = "sk_" + "live_" + "51H" + "abcdefghijklmnopqrstuvwx"  # built at runtime: not a real key
    doc["key_facts"].append(f"Deploy key is {fake_key}")
    sent = {}
    ss.summarize_handoff(doc, api_key="k", post=lambda u, b, k, t: sent.update(b=b) or _reply(GOOD))
    assert fake_key not in json.dumps(sent["b"])


@pytest.mark.parametrize("reply, reason", [
    (_reply(GOOD, finish="length"), "cut off"),
    ({"choices": [{"message": {"content": "no json here"}, "finish_reason": "stop"}]}, "invalid JSON"),
])
def test_unusable_replies_skip_the_summary(reply, reason):
    summary, info = ss.summarize_handoff(_handoff(), api_key="k", post=lambda *a: reply)
    assert summary is None and reason in info["skipped"]


def test_no_key_means_no_call():
    called = []
    summary, info = ss.summarize_handoff(_handoff(), post=lambda *a: called.append(1))
    assert summary is None and info["skipped"] == "no SARVAM_API_KEY" and not called


# --- On the hosted server -----------------------------------------------------

remote = pytest.importorskip("cognicore.connectors.remote")


def _resume(backend):
    from cognicore.memory.context_preservation import resume_session
    return resume_session(backend)


def test_server_stores_the_summary_just_after_the_push_and_reuses_it_until_it_grows(tmp_path):
    from cognicore.memory.context_preservation import import_handoff

    backend = SQLiteMemoryBackend(str(tmp_path / "user.db"))
    calls = []

    def fake(doc):
        calls.append(1)
        return ss.summarize_handoff(doc, api_key="k", post=lambda *a: _reply(GOOD))

    rev = time.time()
    import_handoff(backend, _handoff(), "claude-code:s1", rev)
    assert remote.summarize_synced(backend, "claude-code:s1", _handoff(), rev, summarize=fake) == "summarised"
    with backend._get_conn() as conn:
        row = conn.execute("SELECT timestamp, handoff_json FROM sessions WHERE external_id = 'claude-code:s1'").fetchone()
    assert row["timestamp"] == pytest.approx(rev + 0.001)
    assert "don't need to do step" not in row["handoff_json"]
    assert "Update docs/billing.md for BILL-311" in _resume(backend)

    # A small change: Sarvam is not called again, the cached summary is applied.
    import_handoff(backend, _handoff(31), "claude-code:s1", rev + 10)
    assert remote.summarize_synced(backend, "claude-code:s1", _handoff(31), rev + 10, summarize=fake) == "reused"
    assert len(calls) == 1

    # A newer push that arrives while summarising wins.
    import_handoff(backend, _handoff(32), "claude-code:s1", rev + 20)
    assert "not stored" in remote.summarize_synced(backend, "claude-code:s1", _handoff(32), rev + 15, summarize=fake)


def test_server_leaves_short_and_already_summarised_handoffs_alone(tmp_path, monkeypatch):
    backend = SQLiteMemoryBackend(str(tmp_path / "user.db"))
    never = lambda doc: pytest.fail("should not call Sarvam")  # noqa: E731
    assert remote.summarize_synced(backend, "a", _handoff(0), time.time(), summarize=never) == "short"
    done = {**_handoff(), "compression": {"method": "ai"}}
    assert remote.summarize_synced(backend, "a", done, time.time(), summarize=never) == "already summarised"

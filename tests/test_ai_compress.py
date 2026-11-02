"""Optional AI compression: request shape, safeguards, and hook integration (no network: a fake client)."""
import io
import json
import sqlite3
from types import SimpleNamespace

import pytest

anthropic = pytest.importorskip("anthropic")

from cognicore import hooks  # noqa: E402
from cognicore.memory.ai_compress import HANDOFF_SCHEMA, compress_with_ai  # noqa: E402

SECRET = "sk-test-FAKE0000000000000000000000"

CONV = [
    {"role": "user", "content": "Ticket BILL-311: German totals are off by one cent. We decided to use Decimal "
                                f"with ROUND_HALF_UP because finance requires commercial rounding. Key: {SECRET}"},
    {"role": "assistant", "content": "Fixed total_with_tax in billing.py; tests/test_billing.py passes."},
    {"role": "user", "content": "Next time: format_eur must return '12,34 €'. Reviewer is Anika."},
]

SUMMARY = {
    "goal": "Fix BILL-311 one-cent errors in German totals",
    "current_state": "total_with_tax fixed in billing.py; tests/test_billing.py passes",
    "decisions": [{"what": "Decimal with ROUND_HALF_UP", "why": "finance requires commercial rounding"}],
    "open_tasks": ["format_eur must return '12,34 €'"],
    "key_facts": ["Reviewer: Anika", "Deployed as v9.9.9"],  # v9.9.9 is invented: must be dropped
    "files": ["billing.py", "tests/test_billing.py"],
}


class FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason=self.stop_reason,
            model=kwargs["model"], usage=SimpleNamespace(input_tokens=420, cache_read_input_tokens=0, output_tokens=95))


def fake_client(payload=SUMMARY, stop_reason="end_turn"):
    messages = FakeMessages(payload, stop_reason)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_request_shape_redaction_and_grounding():
    client, messages = fake_client()
    summary, info = compress_with_ai(CONV, model="claude-haiku-5-5", client=client)

    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": HANDOFF_SCHEMA}}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    sent = call["messages"][0]["content"]
    assert SECRET not in sent and "[REDACTED:" in sent and info["redactions"] == 1

    assert summary["key_facts"] == ["Reviewer: Anika"]  # the invented v9.9.9 was dropped
    assert info["dropped_ungrounded"] == 1
    assert info["input_tokens"] == 420 and info["output_tokens"] == 95


@pytest.mark.parametrize("payload,stop,reason", [
    (SUMMARY, "refusal", "refusal"),
    (SUMMARY, "max_tokens", "summary cut off at max_tokens"),
    ("not json", "end_turn", "invalid JSON"),
])
def test_failures_return_none(payload, stop, reason):
    client, _ = fake_client(payload, stop)
    summary, info = compress_with_ai(CONV, client=client)
    assert summary is None and info["skipped"] == reason


def test_api_errors_return_none():
    class Failing:
        def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=None)

    summary, info = compress_with_ai(CONV, client=SimpleNamespace(beta=SimpleNamespace(messages=Failing())))
    assert summary is None and info["skipped"] == "network error"


# ---------------------------------------------------------------------------
# Hooks: compact handoff, call frequency, fallback to rule-based
# ---------------------------------------------------------------------------

def _long_transcript(path, cwd, extra=0):
    filler = [{"type": "user", "message": {"role": "user", "content":
               f"Note {i}: the staging cluster STG-{i} runs release 1.{i}.0 and was checked today."}}
              for i in range(40 + extra)]
    head = [{"type": "user", "message": {"role": "user", "content": m["content"]}} if m["role"] == "user" else
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": m["content"]}]}}
            for m in CONV]
    path.write_text("\n".join(json.dumps(x) for x in head + filler), encoding="utf-8")
    return path


def _save(tmp_path, monkeypatch, transcript, cwd):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s-1", "transcript_path": str(transcript), "cwd": str(cwd)})))
    assert hooks.main(["save"]) == 0


def _brief(tmp_path, monkeypatch, cwd):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(cwd), "source": "startup"})))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    hooks.main(["session-start"])
    return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]


def test_ai_compression_in_hooks(tmp_path, monkeypatch):
    cwd = tmp_path / "billing-app"
    cwd.mkdir()
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "rule"))
    transcript = _long_transcript(tmp_path / "t.jsonl", cwd)
    _save(tmp_path, monkeypatch, transcript, cwd)
    rule_brief = _brief(tmp_path, monkeypatch, cwd)

    calls = []

    def fake(conversation, **kw):
        calls.append(len(conversation))
        return dict(SUMMARY, key_facts=["Reviewer: Anika"]), {"method": "ai", "model": "fake", "input_tokens": 1,
                                                               "output_tokens": 1, "source_tokens": 1}

    monkeypatch.setattr("cognicore.memory.ai_compress.compress_with_ai", fake)
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "ai"))
    monkeypatch.setenv("COGNICORE_AI_COMPRESS", "auto")
    monkeypatch.setenv("COGNICORE_AI_MIN_TOKENS", "300")
    _save(tmp_path, monkeypatch, transcript, cwd)
    ai_brief = _brief(tmp_path, monkeypatch, cwd)

    assert len(ai_brief) < len(rule_brief)
    for value in ("BILL-311", "ROUND_HALF_UP", "commercial rounding", "12,34 €", "Anika", "billing.py"):
        assert value in ai_brief
    assert "sk-test-FAKE" not in ai_brief
    conn = sqlite3.connect(tmp_path / "ai" / "mcp_memories.db")
    doc = json.loads(conn.execute("SELECT handoff_json FROM sessions").fetchone()[0])
    assert doc["compression"]["method"] == "ai"
    values = {v["value"] for v in doc["exact_values"]}
    assert {"STG-0", "STG-39", "release 1.39.0"} <= values  # every value is still kept

    # Stop fires after every turn: a little new conversation reuses the stored summary.
    _save(tmp_path, monkeypatch, transcript, cwd)
    _save(tmp_path, monkeypatch, _long_transcript(transcript, cwd, extra=2), cwd)
    assert len(calls) == 1
    # Much more conversation: summarize again.
    _save(tmp_path, monkeypatch, _long_transcript(transcript, cwd, extra=60), cwd)
    assert len(calls) == 2


def test_ai_failure_falls_back_to_rule_based(tmp_path, monkeypatch):
    cwd = tmp_path / "app"
    cwd.mkdir()
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("COGNICORE_AI_COMPRESS", "always")
    monkeypatch.setattr("cognicore.memory.ai_compress.compress_with_ai",
                        lambda conversation, **kw: (None, {"skipped": "AuthenticationError: no key"}))
    _save(tmp_path, monkeypatch, _long_transcript(tmp_path / "t.jsonl", cwd), cwd)
    brief = _brief(tmp_path, monkeypatch, cwd)
    assert "BILL-311" in brief
    assert "ai compression skipped: AuthenticationError" in (tmp_path / "mem" / "hooks.log").read_text()


def test_ai_off_by_default(tmp_path, monkeypatch):
    cwd = tmp_path / "app"
    cwd.mkdir()
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "mem"))
    monkeypatch.delenv("COGNICORE_AI_COMPRESS", raising=False)
    monkeypatch.setattr("cognicore.memory.ai_compress.compress_with_ai",
                        lambda *a, **k: pytest.fail("model must not be called"))
    _save(tmp_path, monkeypatch, _long_transcript(tmp_path / "t.jsonl", cwd), cwd)

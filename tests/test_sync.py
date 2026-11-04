"""Sync between local memory (Claude Code, Codex) and the hosted account (claude.ai, Desktop, mobile)."""
import json
import threading
import time
import urllib.parse
import urllib.request
from types import SimpleNamespace

import jwt
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from cognicore import hooks, sync  # noqa: E402
from cognicore.connectors import oauth, remote  # noqa: E402
from cognicore.memory.context_preservation import import_handoff, resume_session, save_session  # noqa: E402

ALICE, BOB = "github:1001", "github:2002"
SERVER = "https://memory.test"


def _token(user_id: str) -> str:
    return jwt.encode({"sub": user_id, "typ": "access", "login": user_id, "exp": int(time.time()) + 600},
                      remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM)


def _ctx(user_id):
    request = SimpleNamespace(headers={"Authorization": f"Bearer {_token(user_id)}"}, query_params={})
    return SimpleNamespace(request_context=SimpleNamespace(request=request, meta=None))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")
    monkeypatch.delenv("COGNICORE_DB_PATH", raising=False)
    monkeypatch.delenv("COGNICORE_SYNC", raising=False)
    monkeypatch.delenv("COGNICORE_AI_COMPRESS", raising=False)


@pytest.fixture
def client():
    return TestClient(remote.app)


@pytest.fixture
def local():
    return hooks._backend()


@pytest.fixture
def signed_in(client, monkeypatch):
    """Sync state for ALICE, with the client's HTTP routed into the test app."""
    def fake_request(method, url, body=None, token="", form=False, timeout=0):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if form:
            resp = client.request(method, path, data=body, headers=headers)
        else:
            resp = client.request(method, path, json=body, headers=headers)
        if resp.status_code >= 400:
            raise sync._HTTPError(resp.status_code, resp.text)
        return resp.json()

    monkeypatch.setattr(sync, "_request", fake_request)
    sync.save_state({"server": SERVER, "token_endpoint": f"{SERVER}/token", "client_id": "c1",
                     "access_token": _token(ALICE), "refresh_token": "r1",
                     "expires_at": time.time() + 3600, "login": ALICE})


def _handoff(goal="Fix BILL-311 rounding", project=""):
    return {
        "schema": "cognicore.handoff/v1",
        "provenance": {"session_id": "sess_x", "session_name": "billing-app", "agent": "claude-code",
                       "client": "claude-code-hooks", "created_at": "2026-11-02T10:00:00Z",
                       "message_count": 4, "migrated": False, "project": project},
        "goal": goal, "current_state": "Decimal rounding merged",
        "decisions": [{"what": "Use ROUND_HALF_UP", "why": "finance requires commercial rounding",
                       "status": "made"}],
        "open_tasks": [{"task": "Make format_eur return '12,34 €'", "status": "open"}],
        "key_facts": ["Reviewer is Anika"], "exact_values": [{"value": "BILL-311", "kind": "ticket"}],
        "files": ["billing/tax.py"], "code": [], "environment": {}, "redactions": 0,
    }


# --- import_handoff: the store-as-is path sync uses on both ends -------------

def test_import_creates_then_updates_and_ignores_older_revisions(local):
    first = import_handoff(local, _handoff(), "claude-code:abc", 100.0)
    assert first["status"] == "created"
    assert import_handoff(local, _handoff(goal="newer goal"), "claude-code:abc", 200.0)["status"] == "updated"
    assert import_handoff(local, _handoff(goal="stale goal"), "claude-code:abc", 150.0)["status"] == "unchanged"
    brief = resume_session(local, session_id=first["session_id"]).lower()
    assert "newer goal" in brief and "stale goal" not in brief


def test_import_redacts_secrets_and_rejects_invalid_documents(local):
    doc = _handoff()
    doc["key_facts"].append("Staging key is sk-ant-api03-" + "x" * 40)
    sid = import_handoff(local, doc, "codex:1", 1.0)["session_id"]
    assert "sk-ant-api03" not in resume_session(local, session_id=sid, format="json")
    assert "error" in import_handoff(local, {"goal": "no schema"}, "codex:2", 1.0)
    assert "error" in import_handoff(local, _handoff(), "", 1.0)


# --- server endpoints --------------------------------------------------------

def test_sync_endpoints_require_a_token(client):
    assert client.get("/sync/sessions").status_code == 401
    assert client.put("/sync/sessions", json={}).status_code == 401


def test_push_then_pull_round_trips_and_keeps_users_apart(client):
    alice = {"Authorization": f"Bearer {_token(ALICE)}"}
    put = client.put("/sync/sessions", headers=alice,
                     json={"external_id": "claude-code:abc", "revision": 100.0, "handoff": _handoff()})
    assert put.status_code == 200 and put.json()["status"] == "created"

    got = client.get("/sync/sessions?since=0", headers=alice).json()["sessions"]
    assert [s["external_id"] for s in got] == ["claude-code:abc"]
    assert got[0]["handoff"]["goal"] == "Fix BILL-311 rounding"
    assert client.get("/sync/sessions?since=100", headers=alice).json()["sessions"] == []

    bob = {"Authorization": f"Bearer {_token(BOB)}"}
    assert client.get("/sync/sessions?since=0", headers=bob).json()["sessions"] == []


def test_push_rejects_reserved_ids_bad_bodies_and_oversized_handoffs(client):
    alice = {"Authorization": f"Bearer {_token(ALICE)}"}
    assert client.put("/sync/sessions", headers=alice, json={"external_id": "hosted:sess_1", "revision": 1,
                                                             "handoff": _handoff()}).status_code == 400
    assert client.put("/sync/sessions", headers=alice, json={"revision": 1}).status_code == 400
    big = _handoff()
    big["key_facts"] = ["x" * 1000] * 600
    assert client.put("/sync/sessions", headers=alice, json={"external_id": "codex:big", "revision": 1,
                                                             "handoff": big}).status_code == 413


def test_sessions_saved_through_the_connector_are_listed_with_hosted_ids(client):
    remote.cognicore_save_session(_ctx(ALICE), handoff_json=json.dumps(
        {"goal": "Plan the CogniCore launch posts", "open_tasks": ["Draft the Show HN text"]}), agent="claude-ai")
    got = client.get("/sync/sessions?since=0", headers={"Authorization": f"Bearer {_token(ALICE)}"}).json()
    assert len(got["sessions"]) == 1 and got["sessions"][0]["external_id"].startswith("hosted:")


# --- client: push, pull, refresh, login --------------------------------------

def test_local_session_reaches_the_web_and_web_session_reaches_local(signed_in, local):
    save_session(local, [{"role": "user", "content": "Our goal is to fix BILL-311. We need to update format_eur."}],
                 agent="claude-code", session_key="claude-code:s1", project="/work/billing-app")
    assert sync.push(local, "claude-code:s1")["status"] == "created"
    web = remote.get_backend(_ctx(ALICE))
    assert "BILL-311" in resume_session(web)

    remote.cognicore_save_session(_ctx(ALICE), handoff_json=json.dumps(
        {"goal": "Plan the CogniCore launch posts"}), agent="claude-ai")
    imported = sync.pull(local)
    assert [s["goal"] for s in imported] == ["Plan the CogniCore launch posts"]
    assert "launch posts" in sync.pointer_text(imported, "/work/billing-app")
    assert sync.pull(local) == []  # the local session that came back is not new here
    assert sync.load_state()["last_pull"] > 0


def test_pointer_skips_this_projects_own_sessions():
    from cognicore.memory.context_preservation import project_key

    here = project_key("/work/billing-app")
    assert sync.pointer_text([{"session_id": "s", "session_name": "", "goal": "g", "client": "codex",
                               "project": here}], "/work/billing-app") == ""


def test_expired_access_token_is_refreshed_once_and_saved(signed_in, monkeypatch):
    calls = []
    real = sync._request

    def counting(method, url, body=None, **kw):
        if url.endswith("/token"):
            calls.append(body)
            return {"access_token": "new-access", "refresh_token": "r2", "expires_in": 3600}
        return real(method, url, body, **kw)

    monkeypatch.setattr(sync, "_request", counting)
    sync._update_state(expires_at=0)
    assert sync.access_token() == "new-access"
    assert sync.access_token() == "new-access"
    assert len(calls) == 1 and calls[0]["refresh_token"] == "r1"
    assert sync.load_state()["refresh_token"] == "r2"


def test_rejected_refresh_asks_the_user_to_sign_in_again(signed_in, monkeypatch):
    def rejecting(method, url, body=None, **kw):
        raise sync._HTTPError(400, "refresh token is invalid or expired")

    monkeypatch.setattr(sync, "_request", rejecting)
    sync._update_state(expires_at=0)
    with pytest.raises(sync.SyncError, match="cognicore login"):
        sync.access_token()


def test_login_completes_the_loopback_flow_and_stores_tokens(monkeypatch):
    seen = {}

    def fake_request(method, url, body=None, token="", form=False, timeout=0):
        if url.endswith("/.well-known/oauth-authorization-server"):
            return {"authorization_endpoint": f"{SERVER}/authorize", "token_endpoint": f"{SERVER}/token",
                    "registration_endpoint": f"{SERVER}/register"}
        if url.endswith("/register"):
            seen["redirect"] = body["redirect_uris"][0]
            return {"client_id": "cli-1"}
        if url.endswith("/token"):
            seen["token_body"] = body
            return {"access_token": _token(ALICE), "refresh_token": "r1", "expires_in": 3600}
        raise AssertionError(url)

    monkeypatch.setattr(sync, "_request", fake_request)

    def browser(line):  # plays the user's browser: follow the authorize URL back to the CLI
        if line.startswith("If it doesn't open"):
            url = line.split("\n", 1)[1].strip()
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            seen["authorize"] = q
            target = f"{q['redirect_uri']}?code=the-code&state={q['state']}"
            threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()

    state = sync.login(SERVER, open_browser=False, timeout=10, out=browser)
    assert seen["redirect"].startswith("http://127.0.0.1:") and seen["redirect"].endswith(sync.CALLBACK_PATH)
    assert seen["authorize"]["code_challenge_method"] == "S256"
    assert seen["token_body"]["code"] == "the-code" and seen["token_body"]["code_verifier"]
    assert state["login"] == ALICE and sync.enabled()
    assert sync.logout() and not sync.enabled()


def test_sync_paused_by_environment(signed_in, monkeypatch):
    assert sync.enabled()
    monkeypatch.setenv("COGNICORE_SYNC", "0")
    assert not sync.enabled()


def test_consent_page_names_the_cli_for_its_callback_path():
    assert oauth._client_label("http://127.0.0.1:5555/cognicore-cli/callback")[0] == "CogniCore CLI"
    assert oauth._client_label("http://127.0.0.1:5555/callback")[0] == "Claude Code"


# --- hooks -------------------------------------------------------------------

def test_session_start_shows_new_web_sessions_even_in_a_new_project(signed_in, tmp_path):
    remote.cognicore_save_session(_ctx(ALICE), handoff_json=json.dumps(
        {"goal": "Plan the CogniCore launch posts"}), agent="claude-ai")
    out = hooks.session_start({"cwd": str(tmp_path / "new-project"), "source": "startup"})
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "launch posts" in context and "cognicore_resume_session(session_id=" in context


def test_session_start_still_works_offline(signed_in, monkeypatch, tmp_path):
    def offline(*a, **kw):
        raise sync.SyncError("could not reach memory.test")

    monkeypatch.setattr(sync, "_request", offline)
    assert hooks.session_start({"cwd": str(tmp_path), "source": "startup"}) == ""


def test_save_hook_starts_a_background_push_only_when_something_changed(signed_in, monkeypatch, tmp_path):
    pushed = []
    monkeypatch.setattr(sync, "push_in_background", pushed.append)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {
        "role": "user", "content": "Our goal is to fix BILL-311 today."}}) + "\n", encoding="utf-8")
    event = {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(tmp_path)}
    hooks.save(event)
    hooks.save(event)  # same transcript: deduplicated, nothing to push
    assert pushed == ["claude-code:s1"]


def test_save_hook_does_not_push_when_signed_out(monkeypatch, tmp_path):
    pushed = []
    monkeypatch.setattr(sync, "push_in_background", pushed.append)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {
        "role": "user", "content": "Our goal is to fix BILL-311 today."}}) + "\n", encoding="utf-8")
    hooks.save({"session_id": "s1", "transcript_path": str(transcript), "cwd": str(tmp_path)})
    assert pushed == []


def test_sessions_saved_with_the_local_tool_are_pushed_when_syncing(signed_in, monkeypatch):
    pytest.importorskip("mcp")
    from cognicore.mcp import server

    pushed = []
    monkeypatch.setattr(sync, "push_in_background", pushed.append)
    import asyncio

    app = server.create_mcp_server()
    handoff = json.dumps({"goal": "Recall test", "key_facts": ["The code word is BLUE-HERON-4417"]})
    args = {"handoff_json": handoff, "agent": "claude-code"}
    asyncio.run(app.call_tool("cognicore_save_session", args))
    assert len(pushed) == 1 and pushed[0].startswith("mcp:")
    asyncio.run(app.call_tool("cognicore_save_session", args))  # same content: deduplicated, nothing to push
    assert len(pushed) == 1


# --- Lean plugin, long-session detail, and search over saved sessions -------

def test_search_toolset_loads_only_recall_with_hook_instructions(monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    from cognicore.mcp import server

    monkeypatch.setenv("COGNICORE_TOOLSETS", "search")
    app = server.create_mcp_server()
    assert [t.name for t in asyncio.run(app.list_tools())] == ["cognicore_recall"]
    assert app.instructions == server.HOOKS_INSTRUCTIONS
    monkeypatch.setenv("COGNICORE_TOOLSETS", "search,session")  # overlapping groups keep recall
    names = {t.name for t in asyncio.run(server.create_mcp_server().list_tools())}
    assert {"cognicore_recall", "cognicore_save_session"} <= names


def test_long_session_detail_stays_searchable_but_out_of_the_handoff(local):
    from cognicore.memory.handoff import MAX_FACTS

    facts = [f"Garden{i}a borders lake{i}b near forest{i}c and meadow{i}d." for i in range(MAX_FACTS + 20)]
    save_session(local, [{"role": "user", "content": " ".join(facts)}], session_key="k", project="/p")
    with local._get_conn() as conn:
        handoff = json.loads(conn.execute("SELECT handoff_json FROM sessions").fetchone()[0])
    assert len(handoff["key_facts"]) == MAX_FACTS
    dropped = next(f for f in facts if f not in handoff["key_facts"])
    hits = [r.entry.text for r in local.search(dropped.split()[0], top_k=3)]
    assert any(dropped.split()[0] in h for h in hits)


def test_hosted_recall_finds_facts_from_saved_sessions():
    remote.cognicore_save_session(_ctx(ALICE), handoff_json=json.dumps(
        {"goal": "Recall test", "key_facts": ["The recall test code word is BLUE-HERON-4417"]}))
    assert "BLUE-HERON-4417" in remote.cognicore_recall("recall test code word", _ctx(ALICE))
    assert "BLUE-HERON-4417" in remote.cognicore_recall("BLUE-HERON-4417", _ctx(ALICE))
    assert "BLUE-HERON-4417" not in remote.cognicore_recall("recall test code word", _ctx(BOB))


def test_local_recall_does_not_label_session_facts_as_failures(monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    from cognicore.mcp import server

    monkeypatch.setenv("COGNICORE_TOOLSETS", "memory,session")
    app = server.create_mcp_server()
    asyncio.run(app.call_tool("cognicore_save_session", {"handoff_json": json.dumps(
        {"goal": "Recall test", "key_facts": ["The recall test code word is BLUE-HERON-4417"]})}))
    out = str(asyncio.run(app.call_tool("cognicore_recall", {"query": "recall test code word"})))
    assert "BLUE-HERON-4417" in out and "FAILURE" not in out

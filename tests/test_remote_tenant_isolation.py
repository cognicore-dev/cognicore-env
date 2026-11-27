"""On the hosted server, one signed-in user must not read or write another user's memory.

Users are identified by their GitHub account (``github:<id>``), and GitHub ids are
public, so any tool that takes another user's id is a potential cross-account path.
"""
import json
import time
from types import SimpleNamespace

import jwt
import pytest

from cognicore.connectors import remote

ALICE, BOB = "github:1001", "github:2002"


def _ctx(user_id):
    """An MCP request context as the hosted server sees it: a signed-in HTTP request."""
    token = jwt.encode(
        {"sub": user_id, "typ": "access", "exp": int(time.time()) + 600},
        remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM,
    )
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"}, query_params={})
    return SimpleNamespace(request_context=SimpleNamespace(request=request, meta=None))


def _memories(user_id):
    return [r.entry.text for r in remote.get_backend(_ctx(user_id)).search(query="", top_k=1000, scope=None)]


@pytest.fixture(autouse=True)
def hosted_server(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")
    monkeypatch.delenv("COGNICORE_DB_PATH", raising=False)
    for cache in ("_commerce_db_path", "_agent_registry", "_transaction_ledger", "_reputation_engine"):
        if hasattr(remote, cache):
            monkeypatch.setattr(remote, cache, None)


def test_purchase_requires_seller_to_have_listed():
    remote.cognicore_remember("Alice's private API design notes", _ctx(ALICE), category="secret")
    remote.cognicore_reputation(_ctx(ALICE))  # read-only call that registers Alice

    result = json.loads(remote.cognicore_purchase_memory(_ctx(BOB), seller_agent_id=ALICE))

    assert "not listed" in result["error"]
    assert not any("Alice's private" in text for text in _memories(BOB))


def test_purchase_works_after_seller_opts_in():
    remote.cognicore_remember("Alice's published deployment checklist", _ctx(ALICE), category="ops")
    remote.cognicore_list_for_sale(_ctx(ALICE))

    result = json.loads(remote.cognicore_purchase_memory(_ctx(BOB), seller_agent_id=ALICE, max_price_usd=1000))

    assert "error" not in result
    assert any("deployment checklist" in text for text in _memories(BOB))


def test_share_into_another_users_memory_is_refused():
    before = _memories(ALICE)

    result = json.loads(remote.cognicore_share_experience(query="auth", target_agent_id=ALICE, ctx=_ctx(BOB)))

    assert result["status"] == "refused"
    assert _memories(ALICE) == before


def test_share_within_own_account_is_allowed():
    result = json.loads(remote.cognicore_share_experience(query="auth", target_agent_id=BOB, ctx=_ctx(BOB)))
    assert result["status"] == "transferred"


def test_fabric_tools_run_per_user():
    # These called an undefined _get_backend() and failed on every call.
    out = remote.cognicore_fabric_recommend(_ctx(ALICE), tool_name="figma", task="pick a palette")
    assert "recommendations" in json.loads(out)


def test_team_memory_is_invisible_to_non_members():
    """Knowing a team's id or name gives a non-member nothing: no read, write, delete or log."""
    out = remote.cognicore_team("create", _ctx(ALICE), name="Alice's team")
    team_id = out.split("(id ")[1].split(")")[0]
    shared = remote.cognicore_team("share", _ctx(ALICE), team=team_id, text="Alice's team roadmap is private")
    entry = shared.split("ids=")[1].rstrip(")")
    for ref in (team_id, "Alice's team"):
        for out in (remote.cognicore_team("share", _ctx(BOB), team=ref, text="Bob's note"),
                    remote.cognicore_team("members", _ctx(BOB), team=ref),
                    remote.cognicore_team("audit", _ctx(BOB), team=ref),
                    remote.cognicore_team("invite", _ctx(BOB), team=ref),
                    remote.cognicore_team_remove(ref, _ctx(BOB), delete_team=True)):
            assert out == "Error: No team by that name or id among your teams.", out
    assert remote.cognicore_forget(entry, _ctx(BOB)) == "Error: No team by that name or id among your teams."
    assert "roadmap" not in remote.cognicore_recall("roadmap private", _ctx(BOB))
    assert "roadmap" in remote.cognicore_recall("roadmap private", _ctx(ALICE))


def test_document_upload_link_only_writes_to_its_own_account(monkeypatch):
    """An upload link names one account; it cannot be turned into another account's upload."""
    from fastapi.testclient import TestClient

    from cognicore.connectors import document_upload as up
    from cognicore.memory import documents as docs

    monkeypatch.setenv("COGNICORE_PUBLIC_URL", "https://mcp.example.com")
    link = remote.cognicore_add_document(_ctx(ALICE)).split(": ", 1)[1].split()[0]
    token = link.split("#t=", 1)[1]
    client = TestClient(remote.app)
    # The token decides the account: no header, query or name can redirect it to Bob.
    r = client.put(f"/documents/upload?name=x.txt&user={BOB}", content=b"Alice's private notes",
                   headers={up.UPLOAD_HEADER: token, "Authorization": "Bearer " + jwt.encode(
                       {"sub": BOB, "typ": "access", "exp": int(time.time()) + 600},
                       remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM)})
    assert r.status_code == 200
    assert [d["name"] for d in docs.list_documents(remote.get_backend(_ctx(ALICE)))] == ["x.txt"]
    assert docs.list_documents(remote.get_backend(_ctx(BOB))) == []
    # Bob's access token alone is not an upload token.
    bob_access = jwt.encode({"sub": BOB, "typ": "access", "exp": int(time.time()) + 600},
                            remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM)
    assert client.put("/documents/upload?name=y.txt", content=b"x",
                      headers={up.UPLOAD_HEADER: bob_access}).status_code == 401

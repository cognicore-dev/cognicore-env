"""The Figma sync tools must report a failed sync as failed.

A bad token used to produce "Figma sync complete ... Stored: 0", and
cognicore_fabric_sync_figma called a method that does not exist.
"""
import json
import time
from types import SimpleNamespace

import jwt
import pytest
import requests

from cognicore.connectors import remote

USER = "github:3003"


def _ctx():
    token = jwt.encode(
        {"sub": USER, "typ": "access", "exp": int(time.time()) + 600},
        remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM,
    )
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"}, query_params={})
    return SimpleNamespace(request_context=SimpleNamespace(request=request, meta=None))


def _response(status, body=None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body or {}).encode()
    return resp


@pytest.fixture(autouse=True)
def hosted_server(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")
    monkeypatch.delenv("COGNICORE_DB_PATH", raising=False)
    monkeypatch.delenv("FIGMA_ACCESS_TOKEN", raising=False)
    for cache in ("_commerce_db_path", "_agent_registry", "_transaction_ledger", "_reputation_engine"):
        if hasattr(remote, cache):
            monkeypatch.setattr(remote, cache, None)


def _figma_returns(monkeypatch, status, body=None):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _response(status, body))


def test_rejected_token_is_reported_as_failure(monkeypatch):
    _figma_returns(monkeypatch, 403)
    out = remote.cognicore_figma_sync(file_key="abc", ctx=_ctx(), access_token="bad")
    assert out.startswith("Figma sync failed")
    assert "rejected the access token" in out
    assert "complete" not in out


def test_unknown_file_is_reported_as_failure(monkeypatch):
    _figma_returns(monkeypatch, 404)
    out = remote.cognicore_figma_sync(file_key="nope", ctx=_ctx(), access_token="tok")
    assert out.startswith("Figma sync failed")
    assert "not found" in out


def test_error_message_does_not_echo_the_token(monkeypatch):
    _figma_returns(monkeypatch, 403)
    out = remote.cognicore_figma_sync(file_key="abc", ctx=_ctx(), access_token="figd_secret_value")
    assert "figd_secret_value" not in out


def test_valid_file_still_syncs(monkeypatch):
    _figma_returns(monkeypatch, 200, {"name": "Brand kit", "document": {"children": []}})
    out = remote.cognicore_figma_sync(file_key="abc", ctx=_ctx(), access_token="tok")
    assert "Figma sync complete: 'Brand kit'" in out


def test_fabric_sync_runs_instead_of_calling_a_missing_method(monkeypatch):
    _figma_returns(monkeypatch, 200, {"name": "Brand kit", "document": {"children": []}})
    result = json.loads(remote.cognicore_fabric_sync_figma(_ctx(), file_key="abc", token="tok"))
    assert result["success"] is True
    assert "Brand kit" in result["message"]


def test_fabric_sync_reports_a_rejected_token(monkeypatch):
    _figma_returns(monkeypatch, 403)
    result = json.loads(remote.cognicore_fabric_sync_figma(_ctx(), file_key="abc", token="bad"))
    assert result["success"] is False
    assert "rejected the access token" in result["error"]


def test_fabric_sync_without_any_token_asks_for_one():
    result = json.loads(remote.cognicore_fabric_sync_figma(_ctx(), file_key="abc"))
    assert result["success"] is False
    assert "FIGMA_ACCESS_TOKEN" in result["error"]

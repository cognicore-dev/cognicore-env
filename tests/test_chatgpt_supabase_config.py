"""The ChatGPT connector must not fall back to a hardcoded Supabase project.

It used to default to one project's URL, so a deployment that forgot
SUPABASE_URL silently pointed at someone else's (now deleted) project instead
of failing. AGENTS.md forbids hardcoded fallbacks for configuration.
"""
import pytest
from fastapi.testclient import TestClient

from cognicore.connectors import chatgpt


@pytest.fixture
def client():
    return TestClient(chatgpt.app, raise_server_exceptions=False)


def test_no_hardcoded_project_url_in_the_source():
    source = __import__("inspect").getsource(chatgpt)
    assert "supabase.co" not in source, "a Supabase project URL is hardcoded"


def test_metadata_fails_clearly_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 503
    assert "SUPABASE_URL" in resp.json()["detail"]


def test_metadata_uses_the_configured_project(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example-project.supabase.co/")
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert body["jwks_uri"] == (
        "https://example-project.supabase.co/auth/v1/.well-known/jwks.json"
    )


def test_authorize_redirects_to_the_configured_project(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example-project.supabase.co")
    resp = client.get("/authorize?client_id=abc", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(
        "https://example-project.supabase.co/auth/v1/oauth/authorize?"
    )


def test_authorize_fails_clearly_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    resp = client.get("/authorize?client_id=abc", follow_redirects=False)
    assert resp.status_code == 503

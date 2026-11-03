"""OAuth 2.1 + GitHub sign-in for the remote MCP server (cognicore/connectors/oauth.py).

GitHub is replaced by a stub of ``oauth._http_json``; everything else runs through
the real FastAPI routes and AuthMiddleware.
"""
import base64
import hashlib
import re
import secrets
import urllib.parse

import jwt
import pytest
from fastapi.testclient import TestClient

from cognicore.connectors import oauth, remote

REDIRECT = "http://localhost:53682/callback"
GITHUB_USERS = {"gh-code-alice": (1001, "alice"), "gh-code-bob": (2002, "bob")}


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "gh-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "gh-client-secret")
    monkeypatch.delenv("COGNICORE_PUBLIC_URL", raising=False)

    def fake_http_json(url, data=None, token=None):
        if url.endswith("/login/oauth/access_token"):
            assert data["client_secret"] == "gh-client-secret"
            return {"access_token": f"gho_{data['code']}"} if data["code"] in GITHUB_USERS else {"error": "bad_verification_code"}
        if url.endswith("/user"):
            gh_id, login = GITHUB_USERS[token.removeprefix("gho_")]
            return {"id": gh_id, "login": login}
        raise AssertionError(url)

    monkeypatch.setattr(oauth, "_http_json", fake_http_json)
    monkeypatch.setattr(remote, "oauth_server", oauth.GitHubOAuthServer(remote.JWT_SECRET, remote.JWT_ALGORITHM))


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _authorization_code(client, github_code="gh-code-alice"):
    """Run register -> authorize -> consent -> GitHub -> callback; return (code, verifier, client_id)."""
    client_id = client.post("/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]
    verifier, challenge = _pkce()
    consent = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
    })
    assert consent.status_code == 200 and "Continue with GitHub" in consent.text
    href = re.search(r'href="(/authorize/github\?[^"]+)"', consent.text).group(1).replace("&amp;", "&")
    to_github = client.get(href, follow_redirects=False)
    assert to_github.status_code == 302 and to_github.headers["location"].startswith("https://github.com/login/oauth/authorize?")
    gh_state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(to_github.headers["location"]).query))["state"]
    back = client.get(oauth.CALLBACK_PATH, params={"code": github_code, "state": gh_state}, follow_redirects=False)
    assert back.status_code == 302 and back.headers["location"].startswith(REDIRECT)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(back.headers["location"]).query))
    assert query["state"] == "xyz"
    return query["code"], verifier, client_id


def _exchange(client, code, verifier, client_id, **overrides):
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier, **overrides}
    return client.post("/token", data=form)


def test_oauth_routes_404_when_github_not_configured(monkeypatch):
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setattr(remote, "INSECURE_AUTH_ENABLED", False)
    client = TestClient(remote.app)
    for path in ("/.well-known/oauth-protected-resource", "/authorize/github", oauth.CALLBACK_PATH):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/mcp/sse", "/mcp/stream"])
def test_unauthenticated_mcp_request_points_claude_at_metadata(github, path):
    client = TestClient(remote.app)
    response = client.post(path) if path.endswith("stream") else client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        f'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource{path}"'
    )
    prm = client.get(f"/.well-known/oauth-protected-resource{path}").json()
    assert prm["resource"] == f"http://testserver{path}"
    assert prm["authorization_servers"] == ["http://testserver"]


def test_authorization_server_metadata(github):
    meta = TestClient(remote.app).get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["registration_endpoint"] == "http://testserver/register"
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    assert {"authorization_code", "refresh_token"} <= set(meta["grant_types_supported"])


def test_public_url_and_proxy_headers_set_the_issuer(github, monkeypatch):
    client = TestClient(remote.app)
    proxied = client.get("/.well-known/oauth-authorization-server",
                         headers={"x-forwarded-proto": "https", "x-forwarded-host": "mcp.example.com"})
    assert proxied.json()["issuer"] == "https://mcp.example.com"
    monkeypatch.setenv("COGNICORE_PUBLIC_URL", "https://mcp.cognicoree.tech/")
    assert client.get("/.well-known/oauth-authorization-server").json()["issuer"] == "https://mcp.cognicoree.tech"


def test_full_flow_issues_per_user_github_identity(github):
    client = TestClient(remote.app)
    code, verifier, client_id = _authorization_code(client)
    tokens = _exchange(client, code, verifier, client_id).json()
    claims = jwt.decode(tokens["access_token"], remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])
    assert claims["sub"] == "github:1001" and claims["typ"] == "access"
    assert tokens["token_type"] == "Bearer" and tokens["refresh_token"]

    # The access token gets past the auth check (the bogus session then 4xx's, but not 401).
    ok = client.post("/mcp/messages/?session_id=0", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert ok.status_code != 401

    bob_code, bob_verifier, bob_client = _authorization_code(TestClient(remote.app), "gh-code-bob")
    bob = _exchange(client, bob_code, bob_verifier, bob_client).json()
    bob_sub = jwt.decode(bob["access_token"], remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])["sub"]
    assert bob_sub == "github:2002"
    assert remote.get_db_path_for_user(bob_sub) != remote.get_db_path_for_user(claims["sub"])


def test_code_is_single_use_and_pkce_bound(github):
    client = TestClient(remote.app)
    code, verifier, client_id = _authorization_code(client)
    assert _exchange(client, code, "x" * 50, client_id).json()["error"] == "invalid_grant"
    assert _exchange(client, code, verifier, client_id, redirect_uri="http://localhost:1/x").json()["error"] == "invalid_grant"
    assert _exchange(client, code, verifier, client_id).status_code == 200
    reused = _exchange(client, code, verifier, client_id)
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"


def test_refresh_tokens_rotate_and_cannot_authenticate(github):
    client = TestClient(remote.app)
    code, verifier, client_id = _authorization_code(client)
    first = _exchange(client, code, verifier, client_id).json()
    second = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]})
    assert second.status_code == 200 and second.json()["refresh_token"] != first["refresh_token"]
    replay = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]})
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    for not_access in (first["refresh_token"], code):
        assert client.get("/mcp/sse", headers={"Authorization": f"Bearer {not_access}"}).status_code == 401


def test_untrusted_redirect_uris_are_refused(github):
    client = TestClient(remote.app)
    assert client.post("/register", json={"redirect_uris": ["https://evil.example/cb"]}).status_code == 400
    for uri in ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback",
                "http://127.0.0.1:61234/callback"):
        assert client.post("/register", json={"redirect_uris": [uri]}).status_code == 201
    _, challenge = _pkce()
    evil = client.get("/authorize", params={
        "response_type": "code", "client_id": "c", "redirect_uri": "https://evil.example/cb",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
    }, follow_redirects=False)
    assert evil.status_code == 400 and "location" not in evil.headers


def test_authorize_requires_pkce(github):
    response = TestClient(remote.app).get("/authorize", params={
        "response_type": "code", "client_id": "c", "redirect_uri": REDIRECT, "state": "s",
    }, follow_redirects=False)
    assert response.status_code == 302 and "error=invalid_request" in response.headers["location"]


def test_callback_from_another_browser_is_refused(github):
    attacker = TestClient(remote.app)
    _, challenge = _pkce()
    consent = attacker.get("/authorize", params={
        "response_type": "code", "client_id": "c", "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
    })
    href = re.search(r'href="(/authorize/github\?[^"]+)"', consent.text).group(1).replace("&amp;", "&")
    gh_state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(
        attacker.get(href, follow_redirects=False).headers["location"]).query))["state"]
    victim = TestClient(remote.app)  # no nonce cookie
    response = victim.get(oauth.CALLBACK_PATH, params={"code": "gh-code-alice", "state": gh_state}, follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers


def test_consent_page_cancel_and_security_headers(github):
    _, challenge = _pkce()
    page = TestClient(remote.app).get("/authorize", params={
        "response_type": "code", "client_id": "c", "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st8",
    })
    assert page.status_code == 200 and "Connect Claude to CogniCore Memory" in page.text
    cancel = re.search(r'class="btn secondary" href="([^"]+)"', page.text).group(1).replace("&amp;", "&")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(cancel).query))
    assert cancel.startswith("https://claude.ai/api/mcp/auth_callback?")
    assert query["error"] == "access_denied" and query["state"] == "st8"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("uri", [
    "https://chatgpt.com/connector/oauth/abc123XYZ_-",
    "https://chatgpt.com/connector_platform_oauth_redirect",
])
def test_chatgpt_connector_callbacks_are_accepted(github, uri):
    assert oauth.redirect_uri_allowed(uri)
    assert TestClient(remote.app).post("/register", json={"redirect_uris": [uri]}).status_code == 201


@pytest.mark.parametrize("uri", [
    "https://chatgpt.com.evil.example/connector/oauth/abc",
    "https://evil.example/connector/oauth/abc",
    "http://chatgpt.com/connector/oauth/abc",
    "https://chatgpt.com/connector/oauth/",
    "https://chatgpt.com/connector/oauth/abc/../../evil",
    "https://chatgpt.com/connector/oauth/abc?next=https://evil.example",
    "https://chatgpt.com/other/path",
])
def test_chatgpt_lookalikes_are_refused(uri):
    assert not oauth.redirect_uri_allowed(uri)


def test_consent_page_names_chatgpt(github):
    _, challenge = _pkce()
    page = TestClient(remote.app).get("/authorize", params={
        "response_type": "code", "client_id": "c", "redirect_uri": "https://chatgpt.com/connector/oauth/abc123",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
    })
    assert "Connect ChatGPT to CogniCore Memory" in page.text

"""Sign in with Microsoft (OpenID Connect) on the remote MCP server.

Microsoft is a fake here: an RSA key made in the test signs its ID tokens, its
JWKS is served through ``PyJWKClient.fetch_data``, and its token endpoint
replaces ``oauth._http_json``. Everything on our side runs through the real
FastAPI routes, middleware and token checks.
"""
import base64
import hashlib
import json
import secrets
import time
import urllib.parse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from fastapi.testclient import TestClient

from cognicore.connectors import oauth, remote

REDIRECT = "http://localhost:53682/callback"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_SECRET = "microsoft-client-secret-" + "x" * 24
AUTHORIZE_URL = "https://microsoft.test/authorize"
TOKEN_URL = "https://microsoft.test/token"
JWKS_URL = "https://microsoft.test/keys"
TENANT_ID = "99999999-8888-7777-6666-555555555555"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeMicrosoft:
    """Microsoft's authorization server, as far as our code can observe it."""

    def __init__(self):
        self.key = _rsa_key()
        self.codes = {}           # code -> (code_challenge, id_token)
        self.token_requests = []  # what our server sent to the token endpoint
        self.jwks_fetches = 0

    def jwks(self):
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="k1", alg="RS256", use="sig")
        return {"keys": [jwk]}

    def claims(self, sub, nonce, **overrides):
        now = int(time.time())
        claims = {"iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0", "aud": CLIENT_ID,
                  "sub": sub, "nonce": nonce, "iat": now, "exp": now + 3600, "tid": TENANT_ID}
        claims.update(overrides)
        return {k: v for k, v in claims.items() if v is not None}

    def sign(self, claims, key=None, algorithm="RS256"):
        return jwt.encode(claims, key or self.key, algorithm=algorithm, headers={"kid": "k1"})

    def approve(self, microsoft_params, sub, id_token=None, **overrides):
        """The user picks an account at Microsoft; returns the code Microsoft sends back."""
        code = "M." + secrets.token_urlsafe(8)
        token = id_token(self, microsoft_params["nonce"]) if callable(id_token) else \
            self.sign(self.claims(sub, microsoft_params["nonce"], **overrides))
        self.codes[code] = (microsoft_params["code_challenge"], token)
        return code

    def token_endpoint(self, data):
        self.token_requests.append(data)
        assert data["client_id"] == CLIENT_ID and data["client_secret"] == CLIENT_SECRET
        assert data["grant_type"] == "authorization_code"
        if data["code"] not in self.codes:
            return {"error": "invalid_grant"}
        challenge, id_token = self.codes.pop(data["code"])
        if oauth._pkce_s256(data.get("code_verifier", "")) != challenge:
            return {"error": "invalid_grant", "error_description": "Invalid code verifier."}
        return {"access_token": "EwB.microsoft-access-token", "id_token": id_token,
                "expires_in": 3599, "token_type": "Bearer", "scope": "openid"}


@pytest.fixture
def fake_microsoft():
    return FakeMicrosoft()


def _configure(monkeypatch, fake_microsoft, microsoft=True, github=True):
    monkeypatch.delenv("COGNICORE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    for name, value, on in (
        ("MICROSOFT_CLIENT_ID", CLIENT_ID, microsoft), ("MICROSOFT_CLIENT_SECRET", CLIENT_SECRET, microsoft),
        ("GITHUB_CLIENT_ID", "gh-client-id", github), ("GITHUB_CLIENT_SECRET", "gh-client-secret", github),
    ):
        if on:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COGNICORE_MICROSOFT_AUTHORIZE_URL", AUTHORIZE_URL)
    monkeypatch.setenv("COGNICORE_MICROSOFT_TOKEN_URL", TOKEN_URL)
    monkeypatch.setenv("COGNICORE_MICROSOFT_JWKS_URL", JWKS_URL)

    def fake_http_json(url, data=None, token=None):
        if url == TOKEN_URL:
            return fake_microsoft.token_endpoint(data)
        if url.endswith("/login/oauth/access_token"):
            return {"access_token": "gho_gh-code-123"} if data["code"] == "gh-code-123" else \
                {"error": "bad_verification_code"}
        if url.endswith("/user"):
            return {"id": 123, "login": "octo"}
        raise AssertionError(f"unexpected request to {url}")

    def fake_fetch_data(client):
        assert client.uri == JWKS_URL
        fake_microsoft.jwks_fetches += 1
        return fake_microsoft.jwks()

    monkeypatch.setattr(oauth, "_http_json", fake_http_json)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fake_fetch_data)
    monkeypatch.setattr(remote, "oauth_server", oauth.OAuthServer(remote.JWT_SECRET, remote.JWT_ALGORITHM))


@pytest.fixture
def both(monkeypatch, fake_microsoft):
    _configure(monkeypatch, fake_microsoft)
    return fake_microsoft


@pytest.fixture
def microsoft_only(monkeypatch, fake_microsoft):
    _configure(monkeypatch, fake_microsoft, github=False)
    return fake_microsoft


# ── helpers ──────────────────────────────────────────────────────────────────

def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def _consent(client, state="xyz"):
    client_id = client.post("/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]
    verifier, challenge = _pkce()
    page = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    assert page.status_code == 200
    return page, client_id, verifier


def _button(page, provider):
    import re
    match = re.search(rf'href="(/authorize/{provider}\?[^"]+)"', page.text)
    return match.group(1).replace("&amp;", "&") if match else None


def _to_microsoft(client):
    page, client_id, verifier = _consent(client)
    response = client.get(_button(page, "microsoft"), follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"].startswith(AUTHORIZE_URL + "?")
    return response, client_id, verifier


def _microsoft_sign_in(client, microsoft, sub="o-abc-123", **approve):
    to_microsoft, client_id, verifier = _to_microsoft(client)
    params = _query(to_microsoft.headers["location"])
    code = microsoft.approve(params, sub, **approve)
    back = client.get(oauth.MICROSOFT_CALLBACK_PATH, params={"code": code, "state": params["state"]},
                      follow_redirects=False)
    return back, client_id, verifier, [to_microsoft.headers["location"], back.headers.get("location", "")]


def _exchange(client, code, verifier, client_id, **overrides):
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier, **overrides}
    return client.post("/token", data=form)


def _sub(access_token):
    return jwt.decode(access_token, remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])["sub"]


def _tokens(client, microsoft, sub="o-abc-123"):
    back, client_id, verifier, _ = _microsoft_sign_in(client, microsoft, sub)
    query = _query(back.headers["location"])
    assert query["state"] == "xyz"
    response = _exchange(client, query["code"], verifier, client_id)
    assert response.status_code == 200
    return response.json()


def _assert_sign_in_failed(back):
    assert back.status_code == 302 and back.headers["location"].startswith(REDIRECT)
    query = _query(back.headers["location"])
    assert query == {"error": "server_error", "error_description": "Microsoft sign-in failed", "state": "xyz"}


# ── the happy path and identity rules ────────────────────────────────────────

def test_full_flow_issues_a_microsoft_identity_that_opens_mcp(both):
    client = TestClient(remote.app)
    tokens = _tokens(client, both)
    assert _sub(tokens["access_token"]) == "microsoft:o-abc-123"
    claims = jwt.decode(tokens["access_token"], remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])
    assert claims["typ"] == "access" and claims["login"] == ""
    assert tokens["refresh_token"] and tokens["expires_in"] == oauth.ACCESS_TOKEN_TTL


def test_same_microsoft_account_always_reaches_the_same_memory(both):
    first = _sub(_tokens(TestClient(remote.app), both)["access_token"])
    again = _sub(_tokens(TestClient(remote.app), both)["access_token"])
    assert first == again == "microsoft:o-abc-123"
    assert remote.get_db_path_for_user(first) == remote.get_db_path_for_user(again)


def test_different_microsoft_accounts_get_different_memories(both):
    alice = _sub(_tokens(TestClient(remote.app), both, "111")["access_token"])
    bob = _sub(_tokens(TestClient(remote.app), both, "222")["access_token"])
    assert alice == "microsoft:111" and bob == "microsoft:222"
    assert remote.get_db_path_for_user(alice) != remote.get_db_path_for_user(bob)


def test_microsoft_and_github_accounts_are_never_merged(both):
    """microsoft:123 and github:123 are different people as far as CogniCore knows."""
    microsoft_sub = _sub(_tokens(TestClient(remote.app), both, "123")["access_token"])

    client = TestClient(remote.app)
    page, client_id, verifier = _consent(client)
    to_github = client.get(_button(page, "github"), follow_redirects=False)
    gh_state = _query(to_github.headers["location"])["state"]
    back = client.get(oauth.CALLBACK_PATH, params={"code": "gh-code-123", "state": gh_state}, follow_redirects=False)
    github_sub = _sub(_exchange(client, _query(back.headers["location"])["code"], verifier, client_id)
                      .json()["access_token"])

    assert (microsoft_sub, github_sub) == ("microsoft:123", "github:123")
    assert remote.get_db_path_for_user(microsoft_sub) != remote.get_db_path_for_user(github_sub)


def test_code_is_single_use_and_pkce_bound(both):
    client = TestClient(remote.app)
    back, client_id, verifier, _ = _microsoft_sign_in(client, both)
    code = _query(back.headers["location"])["code"]
    assert _exchange(client, code, "x" * 50, client_id).json()["error"] == "invalid_grant"
    assert _exchange(client, code, verifier, client_id).status_code == 200
    reused = _exchange(client, code, verifier, client_id)
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"


def test_refresh_tokens_rotate_for_microsoft_users(both):
    client = TestClient(remote.app)
    first = _tokens(client, both)
    second = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]})
    assert second.status_code == 200 and _sub(second.json()["access_token"]) == "microsoft:o-abc-123"
    replay = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]})
    assert replay.json()["error"] == "invalid_grant"


# ── what we ask Microsoft for ────────────────────────────────────────────────

def test_request_to_microsoft_asks_for_openid_only_with_pkce_and_nonce(both):
    client = TestClient(remote.app)
    to_microsoft, _, _ = _to_microsoft(client)
    params = _query(to_microsoft.headers["location"])
    assert params["scope"] == "openid"
    assert params["response_type"] == "code"
    assert params["prompt"] == "select_account"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == "http://testserver/oauth/microsoft/callback"
    assert params["code_challenge_method"] == "S256" and len(params["code_challenge"]) == 43
    assert params["nonce"]
    state = jwt.decode(params["state"], remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])
    assert state["typ"] == "microsoftstate" and state["nonce"] == params["nonce"]

    cookie = to_microsoft.headers["set-cookie"]
    assert oauth.NONCE_COOKIE in cookie and "Path=/oauth" in cookie and "HttpOnly" in cookie


def test_pkce_verifier_never_appears_in_a_url(both):
    client = TestClient(remote.app)
    back, _, _, urls = _microsoft_sign_in(client, both)
    assert back.status_code == 302
    verifier = both.token_requests[-1]["code_verifier"]
    assert len(verifier) >= 43
    assert oauth._pkce_s256(verifier) == _query(urls[0])["code_challenge"]
    for url in urls:
        assert verifier not in urllib.parse.unquote(url)


def test_microsoft_tokens_are_not_kept(both):
    client = TestClient(remote.app)
    tokens = _tokens(client, both)
    for ours in (tokens["access_token"], tokens["refresh_token"]):
        claims = jwt.decode(ours, remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])
        assert "EwB" not in json.dumps(claims)
        assert set(claims) <= {"typ", "iat", "exp", "sub", "login", "client_id", "scope", "jti"}


def test_jwks_is_fetched_once_and_cached(both):
    for _ in range(3):
        _tokens(TestClient(remote.app), both)
    assert both.jwks_fetches == 1


# ── ID token validation ──────────────────────────────────────────────────────

def _none_alg(microsoft, nonce):
    claims = microsoft.claims("1", nonce)
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "k1"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


def _hs256_with_client_secret(microsoft, nonce):
    return microsoft.sign(microsoft.claims("1", nonce), key=CLIENT_SECRET, algorithm="HS256")


def _wrong_key(microsoft, nonce):
    return microsoft.sign(microsoft.claims("1", nonce), key=_rsa_key())


def _wrong_nonce(microsoft, nonce):
    return microsoft.sign(microsoft.claims("1", "not-" + nonce))


def _missing_nonce(microsoft, nonce):
    return microsoft.sign(microsoft.claims("1", None))  # claims() drops None values


@pytest.mark.parametrize("id_token, overrides", [
    pytest.param(_wrong_key, {}, id="bad-signature"),
    pytest.param(None, {"iss": "https://evil.example/99999999-8888-7777-6666-555555555555/v2.0"}, id="wrong-host"),
    pytest.param(None, {"iss": "https://login.microsoftonline.com/not-a-guid/v2.0"}, id="non-guid-tenant"),
    pytest.param(None, {"tid": "11111111-1111-1111-1111-111111111111"}, id="tenant-mismatch"),
    pytest.param(None, {"aud": "someone-else"}, id="wrong-aud"),
    pytest.param(None, {"exp": int(time.time()) - 120, "iat": int(time.time()) - 3720}, id="expired"),
    pytest.param(None, {"iat": None}, id="missing-iat"),
    pytest.param(None, {"tid": None}, id="missing-tid"),
    pytest.param(_wrong_nonce, {}, id="wrong-nonce"),
    pytest.param(_missing_nonce, {}, id="missing-nonce"),
    pytest.param(_none_alg, {}, id="alg-none"),
    pytest.param(_hs256_with_client_secret, {}, id="hs256-client-secret"),
    pytest.param(None, {"sub": ""}, id="empty-sub"),
    pytest.param(None, {"sub": None}, id="missing-sub"),
])
def test_invalid_id_tokens_are_rejected(both, id_token, overrides):
    client = TestClient(remote.app)
    back, _, _, _ = _microsoft_sign_in(client, both, id_token=id_token, **overrides)
    _assert_sign_in_failed(back)


def test_expiry_within_leeway_is_accepted(both):
    client = TestClient(remote.app)
    now = int(time.time())
    back, _, _, _ = _microsoft_sign_in(client, both, exp=now - 30, iat=now - 3630)
    assert "code" in _query(back.headers["location"])


def test_a_personal_microsoft_account_tenant_is_accepted(both):
    """The consumers tenant (personal accounts) is just another tenant id."""
    consumers = "9188040d-6c67-4c5b-b112-36a304b66dad"
    back, _, _, _ = _microsoft_sign_in(
        TestClient(remote.app), both, tid=consumers, iss=f"https://login.microsoftonline.com/{consumers}/v2.0")
    assert "code" in _query(back.headers["location"])


@pytest.mark.parametrize("response", [
    {"access_token": "EwB.x"},                 # no id_token
    {"error": "invalid_grant"},
    ["not", "a", "dict"],
])
def test_bad_token_endpoint_responses_fail_cleanly(both, monkeypatch, response):
    monkeypatch.setattr(both, "token_endpoint", lambda data: response)
    back, _, _, _ = _microsoft_sign_in(TestClient(remote.app), both)
    _assert_sign_in_failed(back)


def test_unreachable_microsoft_fails_cleanly(both, monkeypatch):
    def down(data):
        raise OSError("connection refused")
    monkeypatch.setattr(both, "token_endpoint", down)
    back, _, _, _ = _microsoft_sign_in(TestClient(remote.app), both)
    _assert_sign_in_failed(back)


# ── state, nonce cookie and cancellation ─────────────────────────────────────

def test_github_state_is_refused_at_the_microsoft_callback(both):
    client = TestClient(remote.app)
    page, _, _ = _consent(client)
    gh_state = _query(client.get(_button(page, "github"), follow_redirects=False).headers["location"])["state"]
    response = client.get(oauth.MICROSOFT_CALLBACK_PATH, params={"code": "x", "state": gh_state},
                          follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers
    assert not both.token_requests


def test_callback_without_the_nonce_cookie_is_refused(both):
    attacker = TestClient(remote.app)
    to_microsoft, _, _ = _to_microsoft(attacker)
    params = _query(to_microsoft.headers["location"])
    code = both.approve(params, "999")
    victim = TestClient(remote.app)  # a different browser: no cookie
    response = victim.get(oauth.MICROSOFT_CALLBACK_PATH, params={"code": code, "state": params["state"]},
                          follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers
    assert not both.token_requests


def test_user_cancelling_at_microsoft_returns_access_denied_with_client_state(both):
    client = TestClient(remote.app)
    to_microsoft, _, _ = _to_microsoft(client)
    state = _query(to_microsoft.headers["location"])["state"]
    back = client.get(oauth.MICROSOFT_CALLBACK_PATH,
                      params={"error": "access_denied", "state": state}, follow_redirects=False)
    assert back.status_code == 302 and back.headers["location"].startswith(REDIRECT)
    query = _query(back.headers["location"])
    assert query["error"] == "access_denied" and query["state"] == "xyz"
    assert not both.token_requests


# ── which providers are offered ──────────────────────────────────────────────

def test_consent_page_offers_microsoft_with_plain_language(both):
    page, _, _ = _consent(TestClient(remote.app))
    text = page.text
    assert "Continue with Microsoft" in text
    assert "Only your Microsoft account ID, used to keep your memories separate." in text
    assert "No access to your email, OneDrive, Teams or any other Microsoft data." in text


def test_microsoft_not_configured_hides_it_and_github_still_works(monkeypatch, fake_microsoft):
    _configure(monkeypatch, fake_microsoft, microsoft=False)
    client = TestClient(remote.app)
    for path in ("/authorize/microsoft", oauth.MICROSOFT_CALLBACK_PATH):
        assert client.get(path).status_code == 404
    page, client_id, verifier = _consent(client)
    assert "Continue with Microsoft" not in page.text and _button(page, "microsoft") is None
    to_github = client.get(_button(page, "github"), follow_redirects=False)
    gh_state = _query(to_github.headers["location"])["state"]
    back = client.get(oauth.CALLBACK_PATH, params={"code": "gh-code-123", "state": gh_state}, follow_redirects=False)
    tokens = _exchange(client, _query(back.headers["location"])["code"], verifier, client_id).json()
    assert _sub(tokens["access_token"]) == "github:123"


def test_only_microsoft_configured_works_and_github_routes_404(microsoft_only):
    client = TestClient(remote.app)
    for path in ("/authorize/github", oauth.CALLBACK_PATH):
        assert client.get(path).status_code == 404
    assert client.get("/.well-known/oauth-authorization-server").status_code == 200
    page, _, _ = _consent(client)
    assert "Continue with GitHub" not in page.text
    assert "CogniCore receives from Microsoft" in page.text
    assert _sub(_tokens(client, microsoft_only)["access_token"]) == "microsoft:o-abc-123"


def test_microsoft_routes_are_rate_limited(both):
    limited = TestClient(remote.RateLimitMiddleware(remote.app, user_per_minute=0, ip_per_minute=2))
    codes = [limited.get(oauth.MICROSOFT_CALLBACK_PATH).status_code for _ in range(3)]
    assert codes[-1] == 429
    codes = [limited.get("/authorize/microsoft", headers={"X-Real-IP": "203.0.113.9"}).status_code
             for _ in range(3)]
    assert codes[-1] == 429


# ── logs ─────────────────────────────────────────────────────────────────────

def test_no_secret_code_or_token_is_logged(both, capsys, caplog):
    caplog.set_level("DEBUG")
    sub = "o-424242"
    client = TestClient(remote.app)
    back, client_id, verifier, _ = _microsoft_sign_in(client, both, sub)
    our_code = _query(back.headers["location"])["code"]
    tokens = _exchange(client, our_code, verifier, client_id).json()
    _assert_sign_in_failed(_microsoft_sign_in(TestClient(remote.app), both, sub, aud="other")[0])
    _assert_sign_in_failed(_microsoft_sign_in(TestClient(remote.app), both, sub, id_token=_wrong_key)[0])

    out = capsys.readouterr()
    server_records = [r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    logged = out.out + out.err + "\n".join(server_records)
    assert "Microsoft sign-in failed" in logged
    secrets_seen = [CLIENT_SECRET, our_code, tokens["access_token"], tokens["refresh_token"], verifier,
                    "EwB.microsoft-access-token", sub, both.token_requests[0]["code"]]
    for request in both.token_requests:
        secrets_seen += [request["code"], request["code_verifier"]]
    for value in secrets_seen:
        assert value not in logged
    assert "eyJ" not in logged  # no JWT of any kind

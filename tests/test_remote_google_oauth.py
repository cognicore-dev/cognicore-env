"""Sign in with Google (OpenID Connect) on the remote MCP server.

Google is a fake here: an RSA key made in the test signs its ID tokens, its
JWKS is served through ``PyJWKClient.fetch_data``, and its token endpoint
replaces ``oauth._http_json``. Everything on our side runs through the real
FastAPI routes, middleware and token checks.
"""
import base64
import hashlib
import json
import re
import secrets
import time
import urllib.parse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from cognicore.connectors import oauth, remote

REDIRECT = "http://localhost:53682/callback"
CLIENT_ID = "1234-test.apps.googleusercontent.com"
CLIENT_SECRET = "google-client-secret-" + "x" * 24
AUTHORIZE_URL = "https://google.test/o/oauth2/v2/auth"
TOKEN_URL = "https://google.test/token"
JWKS_URL = "https://google.test/certs"
GITHUB_USERS = {"gh-code-123": (123, "octo")}


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeGoogle:
    """Google's authorization server, as far as our code can observe it."""

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
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "azp": CLIENT_ID,
                  "sub": sub, "nonce": nonce, "iat": now, "exp": now + 3600}
        claims.update(overrides)
        return {k: v for k, v in claims.items() if v is not None}

    def sign(self, claims, key=None, algorithm="RS256"):
        return jwt.encode(claims, key or self.key, algorithm=algorithm, headers={"kid": "k1"})

    def approve(self, google_params, sub, id_token=None, **overrides):
        """The user picks an account at Google; returns the code Google sends back."""
        code = "4/google-code-" + secrets.token_urlsafe(8)
        token = id_token(self, google_params["nonce"]) if callable(id_token) else \
            self.sign(self.claims(sub, google_params["nonce"], **overrides))
        self.codes[code] = (google_params["code_challenge"], token)
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
        return {"access_token": "ya29.google-access-token", "id_token": id_token,
                "expires_in": 3599, "token_type": "Bearer", "scope": "openid"}


@pytest.fixture
def fake_google():
    return FakeGoogle()


def _configure(monkeypatch, fake_google, google=True, github=True):
    monkeypatch.delenv("COGNICORE_PUBLIC_URL", raising=False)
    for name, value, on in (("GOOGLE_CLIENT_ID", CLIENT_ID, google), ("GOOGLE_CLIENT_SECRET", CLIENT_SECRET, google),
                            ("GITHUB_CLIENT_ID", "gh-client-id", github),
                            ("GITHUB_CLIENT_SECRET", "gh-client-secret", github)):
        if on:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COGNICORE_GOOGLE_AUTHORIZE_URL", AUTHORIZE_URL)
    monkeypatch.setenv("COGNICORE_GOOGLE_TOKEN_URL", TOKEN_URL)
    monkeypatch.setenv("COGNICORE_GOOGLE_JWKS_URL", JWKS_URL)

    def fake_http_json(url, data=None, token=None):
        if url == TOKEN_URL:
            return fake_google.token_endpoint(data)
        if url.endswith("/login/oauth/access_token"):
            ok = data["code"] in GITHUB_USERS
            return {"access_token": f"gho_{data['code']}"} if ok else {"error": "bad_verification_code"}
        if url.endswith("/user"):
            gh_id, login = GITHUB_USERS[token.removeprefix("gho_")]
            return {"id": gh_id, "login": login}
        raise AssertionError(f"unexpected request to {url}")

    def fake_fetch_data(client):
        assert client.uri == JWKS_URL
        fake_google.jwks_fetches += 1
        return fake_google.jwks()

    monkeypatch.setattr(oauth, "_http_json", fake_http_json)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fake_fetch_data)
    monkeypatch.setattr(remote, "oauth_server", oauth.OAuthServer(remote.JWT_SECRET, remote.JWT_ALGORITHM))


@pytest.fixture
def both(monkeypatch, fake_google):
    _configure(monkeypatch, fake_google)
    return fake_google


@pytest.fixture
def google_only(monkeypatch, fake_google):
    _configure(monkeypatch, fake_google, github=False)
    return fake_google


@pytest.fixture
def github_only(monkeypatch, fake_google):
    _configure(monkeypatch, fake_google, google=False)
    return fake_google


# ── helpers ──────────────────────────────────────────────────────────────────

def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def _consent(client, state="xyz"):
    """register -> /authorize. Returns (consent page, client_id, PKCE verifier)."""
    client_id = client.post("/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]
    verifier, challenge = _pkce()
    page = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    assert page.status_code == 200
    return page, client_id, verifier


def _button(page, provider):
    match = re.search(rf'href="(/authorize/{provider}\?[^"]+)"', page.text)
    return match.group(1).replace("&amp;", "&") if match else None


def _to_google(client):
    """Consent page -> Continue with Google. Returns (redirect to Google, client_id, verifier)."""
    page, client_id, verifier = _consent(client)
    response = client.get(_button(page, "google"), follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"].startswith(AUTHORIZE_URL + "?")
    return response, client_id, verifier


def _google_sign_in(client, google, sub="109876543210", **approve):
    """The whole browser leg. Returns (redirect back to the client, client_id, verifier, [redirect URLs])."""
    to_google, client_id, verifier = _to_google(client)
    params = _query(to_google.headers["location"])
    code = google.approve(params, sub, **approve)
    back = client.get(oauth.GOOGLE_CALLBACK_PATH, params={"code": code, "state": params["state"]},
                      follow_redirects=False)
    return back, client_id, verifier, [to_google.headers["location"], back.headers.get("location", "")]


def _exchange(client, code, verifier, client_id, **overrides):
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier, **overrides}
    return client.post("/token", data=form)


def _sub(access_token):
    return jwt.decode(access_token, remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])["sub"]


def _tokens(client, google, sub="109876543210"):
    back, client_id, verifier, _ = _google_sign_in(client, google, sub)
    query = _query(back.headers["location"])
    assert query["state"] == "xyz"
    response = _exchange(client, query["code"], verifier, client_id)
    assert response.status_code == 200
    return response.json()


def _assert_sign_in_failed(back):
    assert back.status_code == 302 and back.headers["location"].startswith(REDIRECT)
    query = _query(back.headers["location"])
    assert query == {"error": "server_error", "error_description": "Google sign-in failed", "state": "xyz"}


def _protected_app():
    async def ok(request):
        return PlainTextResponse("ok")
    stub = Starlette(routes=[Route("/{path:path}", ok, methods=["GET", "POST"])])
    return TestClient(remote.AuthMiddleware(stub))


# ── the happy path and identity rules ────────────────────────────────────────

def test_full_flow_issues_a_google_identity_that_opens_mcp(both):
    client = TestClient(remote.app)
    tokens = _tokens(client, both)
    assert _sub(tokens["access_token"]) == "google:109876543210"
    claims = jwt.decode(tokens["access_token"], remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])
    assert claims["typ"] == "access" and claims["login"] == ""
    assert tokens["refresh_token"] and tokens["expires_in"] == oauth.ACCESS_TOKEN_TTL

    mcp = _protected_app()
    bearer = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert mcp.post("/mcp/stream", headers=bearer).status_code == 200
    assert mcp.post("/mcp/stream").status_code == 401

    class Req:
        headers = bearer
    assert remote.get_user_id(Req()) == "google:109876543210"


def test_same_google_account_always_reaches_the_same_memory(both):
    """Signing in again, from any client app, lands in the same database."""
    first = _sub(_tokens(TestClient(remote.app), both)["access_token"])
    again = _sub(_tokens(TestClient(remote.app), both)["access_token"])
    assert first == again == "google:109876543210"
    assert remote.get_db_path_for_user(first) == remote.get_db_path_for_user(again)


def test_different_google_accounts_get_different_memories(both):
    alice = _sub(_tokens(TestClient(remote.app), both, "111")["access_token"])
    bob = _sub(_tokens(TestClient(remote.app), both, "222")["access_token"])
    assert alice == "google:111" and bob == "google:222"
    assert remote.get_db_path_for_user(alice) != remote.get_db_path_for_user(bob)


def test_google_and_github_accounts_are_never_merged(both):
    """google:123 and github:123 are different people as far as CogniCore knows."""
    google_sub = _sub(_tokens(TestClient(remote.app), both, "123")["access_token"])

    client = TestClient(remote.app)
    page, client_id, verifier = _consent(client)
    to_github = client.get(_button(page, "github"), follow_redirects=False)
    gh_state = _query(to_github.headers["location"])["state"]
    back = client.get(oauth.CALLBACK_PATH, params={"code": "gh-code-123", "state": gh_state}, follow_redirects=False)
    github_sub = _sub(_exchange(client, _query(back.headers["location"])["code"], verifier, client_id)
                      .json()["access_token"])

    assert (google_sub, github_sub) == ("google:123", "github:123")
    assert remote.get_db_path_for_user(google_sub) != remote.get_db_path_for_user(github_sub)


def test_code_is_single_use_and_pkce_bound(both):
    client = TestClient(remote.app)
    back, client_id, verifier, _ = _google_sign_in(client, both)
    code = _query(back.headers["location"])["code"]
    assert _exchange(client, code, "x" * 50, client_id).json()["error"] == "invalid_grant"
    assert _exchange(client, code, verifier, client_id, redirect_uri="http://localhost:1/x").json()["error"] == \
        "invalid_grant"
    assert _exchange(client, code, verifier, client_id).status_code == 200
    reused = _exchange(client, code, verifier, client_id)
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"


def test_refresh_tokens_rotate_for_google_users(both):
    client = TestClient(remote.app)
    first = _tokens(client, both)
    second = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]})
    assert second.status_code == 200 and _sub(second.json()["access_token"]) == "google:109876543210"
    replay = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]})
    assert replay.json()["error"] == "invalid_grant"


# ── what we ask Google for ───────────────────────────────────────────────────

def test_request_to_google_asks_for_openid_only_with_pkce_and_nonce(both):
    client = TestClient(remote.app)
    to_google, _, _ = _to_google(client)
    params = _query(to_google.headers["location"])
    assert params["scope"] == "openid"
    assert params["response_type"] == "code"
    assert params["prompt"] == "select_account"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == "http://testserver/oauth/google/callback"
    assert params["code_challenge_method"] == "S256" and len(params["code_challenge"]) == 43
    assert params["nonce"]
    state = jwt.decode(params["state"], remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])
    assert state["typ"] == "googlestate" and state["nonce"] == params["nonce"]

    cookie = to_google.headers["set-cookie"]
    assert oauth.NONCE_COOKIE in cookie and "Path=/oauth" in cookie and "HttpOnly" in cookie


def test_pkce_verifier_never_appears_in_a_url(both):
    client = TestClient(remote.app)
    back, _, _, urls = _google_sign_in(client, both)
    assert back.status_code == 302
    verifier = both.token_requests[-1]["code_verifier"]
    assert len(verifier) >= 43
    assert oauth._pkce_s256(verifier) == _query(urls[0])["code_challenge"]
    for url in urls:
        assert verifier not in urllib.parse.unquote(url)


def test_google_tokens_are_not_kept(both):
    """Google's access and ID tokens must not end up in anything we issue."""
    client = TestClient(remote.app)
    tokens = _tokens(client, both)
    for ours in (tokens["access_token"], tokens["refresh_token"]):
        claims = jwt.decode(ours, remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])
        assert "ya29" not in json.dumps(claims)
        assert set(claims) <= {"typ", "iat", "exp", "sub", "login", "client_id", "scope", "jti"}


def test_jwks_is_fetched_once_and_cached(both):
    for _ in range(3):
        _tokens(TestClient(remote.app), both)
    assert both.jwks_fetches == 1


# ── ID token validation ──────────────────────────────────────────────────────

def _none_alg(google, nonce):
    claims = google.claims("1", nonce)
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "k1"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


def _hs256_with_client_secret(google, nonce):
    return google.sign(google.claims("1", nonce), key=CLIENT_SECRET, algorithm="HS256")


def _hs256_with_public_key(google, nonce):
    from cryptography.hazmat.primitives import serialization
    pem = google.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "kid": "k1", "typ": "JWT"}).encode())
    body = base64.urlsafe_b64encode(json.dumps(google.claims("1", nonce)).encode())
    signing_input = header.rstrip(b"=") + b"." + body.rstrip(b"=")
    import hmac as _hmac
    sig = base64.urlsafe_b64encode(_hmac.new(pem, signing_input, hashlib.sha256).digest()).rstrip(b"=")
    return (signing_input + b"." + sig).decode()


def _wrong_key(google, nonce):
    return google.sign(google.claims("1", nonce), key=_rsa_key())


def _wrong_nonce(google, nonce):
    return google.sign(google.claims("1", "not-" + nonce))


def _missing_nonce(google, nonce):
    return google.sign(google.claims("1", None))  # claims() drops None values


@pytest.mark.parametrize("id_token, overrides", [
    pytest.param(_wrong_key, {}, id="bad-signature"),
    pytest.param(None, {"iss": "https://accounts.evil.example"}, id="wrong-iss"),
    pytest.param(None, {"aud": "someone-else.apps.googleusercontent.com"}, id="wrong-aud"),
    pytest.param(None, {"aud": [CLIENT_ID, "other-client"]}, id="list-aud"),
    pytest.param(None, {"azp": "someone-else.apps.googleusercontent.com"}, id="wrong-azp"),
    pytest.param(None, {"exp": int(time.time()) - 120, "iat": int(time.time()) - 3720}, id="expired"),
    pytest.param(None, {"iat": int(time.time()) + 600, "exp": int(time.time()) + 4200}, id="issued-in-future"),
    pytest.param(None, {"iat": None}, id="missing-iat"),
    pytest.param(_wrong_nonce, {}, id="wrong-nonce"),
    pytest.param(_missing_nonce, {}, id="missing-nonce"),
    pytest.param(_none_alg, {}, id="alg-none"),
    pytest.param(_hs256_with_client_secret, {}, id="hs256-client-secret"),
    pytest.param(_hs256_with_public_key, {}, id="hs256-public-key"),
    pytest.param(None, {"sub": ""}, id="empty-sub"),
    pytest.param(None, {"sub": None}, id="missing-sub"),
])
def test_invalid_id_tokens_are_rejected(both, id_token, overrides):
    client = TestClient(remote.app)
    back, _, _, _ = _google_sign_in(client, both, id_token=id_token, **overrides)
    _assert_sign_in_failed(back)


def test_expiry_within_leeway_is_accepted(both):
    client = TestClient(remote.app)
    now = int(time.time())
    back, _, _, _ = _google_sign_in(client, both, exp=now - 30, iat=now - 3630)
    assert "code" in _query(back.headers["location"])


@pytest.mark.parametrize("iss", ["https://accounts.google.com", "accounts.google.com"])
def test_both_google_issuer_spellings_are_accepted(both, iss):
    back, _, _, _ = _google_sign_in(TestClient(remote.app), both, iss=iss)
    assert "code" in _query(back.headers["location"])


@pytest.mark.parametrize("response", [
    {"access_token": "ya29.x"},                 # no id_token
    {"error": "invalid_grant"},
    ["not", "a", "dict"],
])
def test_bad_token_endpoint_responses_fail_cleanly(both, monkeypatch, response):
    monkeypatch.setattr(both, "token_endpoint", lambda data: response)
    back, _, _, _ = _google_sign_in(TestClient(remote.app), both)
    _assert_sign_in_failed(back)


def test_unreachable_google_fails_cleanly(both, monkeypatch):
    def down(data):
        raise OSError("connection refused")
    monkeypatch.setattr(both, "token_endpoint", down)
    back, _, _, _ = _google_sign_in(TestClient(remote.app), both)
    _assert_sign_in_failed(back)


# ── state, nonce cookie and cancellation ─────────────────────────────────────

def test_github_state_is_refused_at_the_google_callback(both):
    client = TestClient(remote.app)
    page, _, _ = _consent(client)
    gh_state = _query(client.get(_button(page, "github"), follow_redirects=False).headers["location"])["state"]
    response = client.get(oauth.GOOGLE_CALLBACK_PATH, params={"code": "4/x", "state": gh_state},
                          follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers
    assert not both.token_requests


def test_google_state_is_refused_at_the_github_callback(both):
    client = TestClient(remote.app)
    to_google, _, _ = _to_google(client)
    google_state = _query(to_google.headers["location"])["state"]
    response = client.get(oauth.CALLBACK_PATH, params={"code": "gh-code-123", "state": google_state},
                          follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers


def test_callback_without_the_nonce_cookie_is_refused(both):
    attacker = TestClient(remote.app)
    to_google, _, _ = _to_google(attacker)
    params = _query(to_google.headers["location"])
    code = both.approve(params, "999")
    victim = TestClient(remote.app)  # a different browser: no cookie
    response = victim.get(oauth.GOOGLE_CALLBACK_PATH, params={"code": code, "state": params["state"]},
                          follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers
    assert not both.token_requests


def test_callback_with_a_mismatched_nonce_cookie_is_refused(both):
    client = TestClient(remote.app)
    to_google, _, _ = _to_google(client)
    params = _query(to_google.headers["location"])
    client.cookies.clear()
    client.cookies.set(oauth.NONCE_COOKIE, "some-other-nonce")
    response = client.get(oauth.GOOGLE_CALLBACK_PATH, params={"code": both.approve(params, "1"),
                                                             "state": params["state"]}, follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers


def test_expired_state_is_refused(both):
    client = TestClient(remote.app)
    to_google, _, _ = _to_google(client)
    params = _query(to_google.headers["location"])
    real = jwt.decode(params["state"], remote.JWT_SECRET, algorithms=[remote.JWT_ALGORITHM])
    # The same state, signed by the server, but past its lifetime.
    expired = remote.oauth_server._sign("googlestate", -60, req=real["req"], nonce=real["nonce"])
    response = client.get(oauth.GOOGLE_CALLBACK_PATH, params={"code": both.approve(params, "1"),
                                                             "state": expired}, follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers


def test_expired_consent_link_is_refused(both):
    client = TestClient(remote.app)
    response = client.get("/authorize/google", params={"request": "not-a-signed-request"}, follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers


def test_user_cancelling_at_google_returns_access_denied_with_client_state(both):
    client = TestClient(remote.app)
    to_google, _, _ = _to_google(client)
    state = _query(to_google.headers["location"])["state"]
    back = client.get(oauth.GOOGLE_CALLBACK_PATH,
                      params={"error": "access_denied", "error_description": "<script>", "state": state},
                      follow_redirects=False)
    assert back.status_code == 302 and back.headers["location"].startswith(REDIRECT)
    query = _query(back.headers["location"])
    assert query["error"] == "access_denied" and query["state"] == "xyz"
    assert "<script>" not in query["error_description"]
    assert not both.token_requests


# ── which providers are offered ──────────────────────────────────────────────

def test_consent_page_offers_google_first_with_plain_language(both):
    page, _, _ = _consent(TestClient(remote.app))
    text = page.text
    assert text.index("Continue with Google") < text.index("Continue with GitHub")
    assert "Only your Google account ID, used to keep your memories separate." in text
    assert "No access to your email, contacts, Drive or any other Google data." in text
    # Reassurance that applies to both providers, not only Google.
    assert "No new account or password needed." in text
    assert "keeps its own memories" in text
    assert "Google and GitHub are separate accounts." in text
    for jargon in ("OAuth", "token", "scope", "OIDC", "PKCE"):
        assert jargon not in re.sub(r"<[^>]+>", " ", text.split("<body>")[1])
    # The official multicolour G.
    for colour in ("#EA4335", "#4285F4", "#FBBC05", "#34A853"):
        assert colour in text


def test_each_disclosure_sits_under_its_own_button(both):
    """What a provider shares is read at the moment of choosing it, not in a list elsewhere."""
    text, _, _ = _consent(TestClient(remote.app))
    text = text.text
    google_btn, github_btn = text.index("Continue with Google"), text.index("Continue with GitHub")
    google_says = text.index("Only your Google account ID")
    github_says = text.index("Your public GitHub username and account ID")
    assert google_btn < google_says < github_btn < github_says


def test_buttons_come_before_the_fine_print(both):
    """The page must not need a scroll to reach the choice."""
    text = _consent(TestClient(remote.app))[0].text
    assert text.index("Continue with Google") < text.index("separate accounts") < text.index(">Cancel<")


def test_motion_is_css_only_and_respects_reduced_motion(both):
    text = _consent(TestClient(remote.app))[0].text
    assert "<script" not in text.lower()
    assert "prefers-reduced-motion:reduce" in text
    assert "@keyframes" in text


def test_loopback_warning_still_shown(both):
    page, _, _ = _consent(TestClient(remote.app))
    assert "Any program on this computer can request a local sign-in." in page.text


def test_google_not_configured_hides_it_and_github_still_works(github_only):
    client = TestClient(remote.app)
    for path in ("/authorize/google", oauth.GOOGLE_CALLBACK_PATH):
        assert client.get(path).status_code == 404
    page, client_id, verifier = _consent(client)
    assert "Continue with Google" not in page.text and _button(page, "google") is None
    assert "CogniCore receives from GitHub" in page.text
    to_github = client.get(_button(page, "github"), follow_redirects=False)
    gh_state = _query(to_github.headers["location"])["state"]
    back = client.get(oauth.CALLBACK_PATH, params={"code": "gh-code-123", "state": gh_state}, follow_redirects=False)
    tokens = _exchange(client, _query(back.headers["location"])["code"], verifier, client_id).json()
    assert _sub(tokens["access_token"]) == "github:123"


def test_only_google_configured_works_and_github_routes_404(google_only):
    client = TestClient(remote.app)
    for path in ("/authorize/github", oauth.CALLBACK_PATH):
        assert client.get(path).status_code == 404
    assert client.get("/.well-known/oauth-authorization-server").status_code == 200
    assert client.get("/.well-known/oauth-protected-resource/mcp/stream").status_code == 200
    page, _, _ = _consent(client)
    assert "Continue with GitHub" not in page.text
    assert "CogniCore receives from Google" in page.text
    assert _sub(_tokens(client, google_only)["access_token"]) == "google:109876543210"


def test_nothing_configured_hides_the_whole_server(monkeypatch, fake_google):
    _configure(monkeypatch, fake_google, google=False, github=False)
    monkeypatch.setattr(remote, "INSECURE_AUTH_ENABLED", False)
    client = TestClient(remote.app)
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource",
                 "/authorize", "/authorize/google", oauth.GOOGLE_CALLBACK_PATH):
        assert client.get(path).status_code == 404


def test_google_routes_are_rate_limited(both):
    limited = TestClient(remote.RateLimitMiddleware(remote.app, user_per_minute=0, ip_per_minute=2))
    codes = [limited.get(oauth.GOOGLE_CALLBACK_PATH).status_code for _ in range(3)]
    assert codes[-1] == 429
    codes = [limited.get("/authorize/google", headers={"X-Real-IP": "203.0.113.9"}).status_code for _ in range(3)]
    assert codes[-1] == 429


# ── logs ─────────────────────────────────────────────────────────────────────

def test_no_secret_code_or_token_is_logged(both, capsys, caplog):
    caplog.set_level("DEBUG")
    sub = "424242424242"
    client = TestClient(remote.app)
    back, client_id, verifier, _ = _google_sign_in(client, both, sub)
    our_code = _query(back.headers["location"])["code"]
    tokens = _exchange(client, our_code, verifier, client_id).json()
    # And a failing sign-in, which is what writes a log line.
    _assert_sign_in_failed(_google_sign_in(TestClient(remote.app), both, sub, aud="other")[0])
    _assert_sign_in_failed(_google_sign_in(TestClient(remote.app), both, sub, id_token=_wrong_key)[0])

    out = capsys.readouterr()
    # The test client's own request log (httpx) is not the server's.
    server_records = [r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    logged = out.out + out.err + "\n".join(server_records)
    assert "Google sign-in failed" in logged
    secrets_seen = [CLIENT_SECRET, our_code, tokens["access_token"], tokens["refresh_token"], verifier,
                    "ya29.google-access-token", sub, both.token_requests[0]["code"]]
    for request in both.token_requests:
        secrets_seen += [request["code"], request["code_verifier"]]
    for value in secrets_seen:
        assert value not in logged
    assert "eyJ" not in logged  # no JWT of any kind

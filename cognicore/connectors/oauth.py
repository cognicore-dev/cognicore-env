"""OAuth 2.1 authorization server for the remote MCP server, with Google and GitHub sign-in.

Claude (claude.ai, Desktop, mobile, Claude Code) connects to a remote MCP server
through OAuth: it reads protected-resource metadata, registers itself with
Dynamic Client Registration, sends the user to ``/authorize`` with PKCE, and
exchanges the code at ``/token``. This module implements that server side and
delegates the actual sign-in to Google or GitHub, so every user gets a stable,
distinct identity (``google:<sub>`` or ``github:<numeric id>``) without running a
user database. The two are never linked: the same person signing in with each
gets two separate accounts.

Everything is stateless except a small in-memory set of consumed code and
refresh-token IDs. Requests, codes and tokens are JWTs signed with the server's
``COGNICORE_JWT_SECRET`` and told apart by their ``typ`` claim. The GitHub
access token is used once to read the user's ID and is never stored. Google
sign-in is OpenID Connect with only the ``openid`` scope: the ID token is
verified, its ``sub`` read, and nothing from Google is stored.

Each provider is enabled by its ``<PROVIDER>_CLIENT_ID`` and
``<PROVIDER>_CLIENT_SECRET``; the server is enabled when at least one is.
``COGNICORE_PUBLIC_URL`` should be set to the server's public origin
(e.g. ``https://mcp.example.com``) so the issuer is stable behind proxies.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
from typing import Any

import jwt
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.formparsers import MultiPartException

ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
CODE_TTL = 300
REQUEST_TTL = 600

SCOPES = ["mcp", "offline_access"]
NONCE_COOKIE = "cognicore_oauth_nonce"
CALLBACK_PATH = "/oauth/github/callback"
GITHUB_CALLBACK_PATH = CALLBACK_PATH
GOOGLE_CALLBACK_PATH = "/oauth/google/callback"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
# Clock skew allowed when checking a Google ID token's exp and iat.
GOOGLE_LEEWAY = 60

# Redirect URIs Claude uses. Claude Code uses an RFC 8252 loopback redirect on
# an ephemeral port, accepted separately below.
CLAUDE_REDIRECT_URIS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# ChatGPT gives every connector its own callback, https://chatgpt.com/connector/oauth/<id>,
# plus a legacy shared one. chatgpt.com is controlled by OpenAI, so any id is accepted.
CHATGPT_REDIRECT = re.compile(r"^https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,128}$")
CHATGPT_LEGACY_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
# Cursor registers all its callbacks in one request, so one unknown URI fails the whole
# registration: the desktop app's custom scheme, plus cursor.com callbacks for cloud
# agents (/agents/) and the Slack/Grok bot (/bot/). Desktop also uses a loopback callback.
CURSOR_DESKTOP_REDIRECT = "cursor://anysphere.cursor-mcp/oauth/callback"
CURSOR_WEB_REDIRECT = re.compile(r"^https://(www\.)?cursor\.com/(agents|bot)/mcp/oauth/callback$")

# MCP endpoints a user can enter as the connector URL. Protected-resource
# metadata must name the exact URL the user entered.
SSE_RESOURCE_PATH = "/mcp/sse"
STREAM_RESOURCE_PATH = "/mcp/stream"


def github_enabled() -> bool:
    return bool(os.environ.get("GITHUB_CLIENT_ID") and os.environ.get("GITHUB_CLIENT_SECRET"))


def google_enabled() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def oauth_enabled() -> bool:
    """The authorization server runs when at least one sign-in provider is configured."""
    return github_enabled() or google_enabled()


def _github_oauth_base() -> str:
    return os.environ.get("COGNICORE_GITHUB_OAUTH_BASE", "https://github.com").rstrip("/")


def _github_api_base() -> str:
    return os.environ.get("COGNICORE_GITHUB_API_BASE", "https://api.github.com").rstrip("/")


# Google's endpoints, overridable so tests (and staging) can point them elsewhere.
def _google_authorize_url() -> str:
    return os.environ.get("COGNICORE_GOOGLE_AUTHORIZE_URL", "https://accounts.google.com/o/oauth2/v2/auth")


def _google_token_url() -> str:
    return os.environ.get("COGNICORE_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")


def _google_jwks_url() -> str:
    return os.environ.get("COGNICORE_GOOGLE_JWKS_URL", "https://www.googleapis.com/oauth2/v3/certs")


def base_url_from_headers(headers: dict[str, str], scheme: str, host: str) -> str:
    """Public origin of this server: COGNICORE_PUBLIC_URL, else proxy headers, else the request."""
    configured = os.environ.get("COGNICORE_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    proto = headers.get("x-forwarded-proto", scheme).split(",")[0].strip()
    fwd_host = headers.get("x-forwarded-host", host).split(",")[0].strip()
    return f"{proto}://{fwd_host}"


def base_url(request: Request) -> str:
    return base_url_from_headers(
        {k.lower(): v for k, v in request.headers.items()},
        request.url.scheme,
        request.headers.get("host", request.url.netloc),
    )


def base_url_from_scope(scope: dict[str, Any]) -> str:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    server = scope.get("server") or ("localhost", 80)
    host = headers.get("host") or f"{server[0]}:{server[1]}"
    return base_url_from_headers(headers, scope.get("scheme", "http"), host)


def resource_path_for(request_path: str) -> str:
    """The connector URL path a request belongs to, for the WWW-Authenticate pointer."""
    if request_path.startswith(STREAM_RESOURCE_PATH):
        return STREAM_RESOURCE_PATH
    return SSE_RESOURCE_PATH


def www_authenticate(base: str, request_path: str) -> str:
    resource = resource_path_for(request_path)
    return f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource{resource}"'


def redirect_uri_allowed(uri: str) -> bool:
    if not uri:
        return False
    extra = {u.strip() for u in os.environ.get("COGNICORE_EXTRA_REDIRECT_URIS", "").split(",") if u.strip()}
    if uri in CLAUDE_REDIRECT_URIS or uri in extra:
        return True
    if uri == CHATGPT_LEGACY_REDIRECT or CHATGPT_REDIRECT.match(uri):
        return True
    if uri == CURSOR_DESKTOP_REDIRECT or CURSOR_WEB_REDIRECT.match(uri):
        return True
    parsed = urllib.parse.urlparse(uri)
    # RFC 8252 loopback redirect: any port, matched for localhost and 127.0.0.1.
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS


def is_access_token_payload(payload: dict[str, Any]) -> bool:
    """Access tokens are untyped (legacy) or typ=access. Codes, refresh tokens and
    request blobs are signed with the same key and must never authenticate a call."""
    return payload.get("typ") in (None, "access")


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _with_query(uri: str, params: dict[str, str]) -> str:
    sep = "&" if urllib.parse.urlparse(uri).query else "?"
    return f"{uri}{sep}{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# -- sign-in pages ---------------------------------------------------------------
# Self-contained (no external assets) so the strict CSP below holds.

_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}

_PAGE_CSS = """
:root{--bg:#fafaf9;--card:#fff;--text:#0c0c0d;--muted:#6b6b70;--faint:#8e8e93;--line:#e8e7e4;--line-strong:#dadce0;--soft:#f5f5f3;--focus:#4f46e5;
--warn-bg:#fff8eb;--warn-line:#f1d9a6;--warn-text:#7a4b00;
--shadow:0 1px 1px rgba(0,0,0,.02),0 2px 4px rgba(0,0,0,.03),0 16px 40px -12px rgba(0,0,0,.12)}
@media (prefers-color-scheme:dark){:root{--bg:#09090a;--card:#121214;--text:#ededef;--muted:#9a9aa2;--faint:#6e6e76;
--line:#232327;--line-strong:#3c4043;--soft:#19191c;--focus:#a5b4fc;
--warn-bg:#211a0d;--warn-line:#4a3a17;--warn-text:#f1c878;
--shadow:0 0 0 1px rgba(255,255,255,.02),0 24px 48px -16px rgba(0,0,0,.6)}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:32px 16px;
background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
main{width:100%;max-width:400px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px;box-shadow:var(--shadow)}
.marks{display:flex;align-items:center;gap:10px;margin-bottom:22px}
.mark{width:40px;height:40px;border-radius:10px;display:grid;place-items:center;flex:none}
.mark.brand{background:var(--text);color:var(--card)}
.mark.app{background:var(--soft);border:1px solid var(--line);color:var(--muted)}
.mark.alert{background:var(--warn-bg);border:1px solid var(--warn-line);color:var(--warn-text)}
.link{position:relative;width:40px;height:1.5px;border-radius:2px;background:var(--line);overflow:hidden}
.link::after{content:"";position:absolute;inset:0 auto 0 0;width:14px;border-radius:2px;
background:linear-gradient(90deg,transparent,var(--text));animation:flow 2.6s cubic-bezier(.65,0,.35,1) infinite}
h1{font-size:19px;line-height:1.3;font-weight:620;letter-spacing:-.015em;margin:0 0 6px}
.lede{color:var(--muted);margin:0 0 22px;font-size:14.5px}
.actions{display:grid;gap:14px}
.option{display:grid;gap:7px}
.btn{position:relative;display:flex;align-items:center;justify-content:center;gap:10px;height:44px;padding:0 16px;
border-radius:10px;font-weight:560;font-size:14.5px;text-decoration:none;
transition:background-color .15s ease,border-color .15s ease,transform .12s ease}
.btn:active{transform:scale(.985)}
.btn svg{flex:none}
.btn.outline{background:var(--card);color:var(--text);border:1px solid var(--line-strong)}
.btn.outline:hover{background:var(--soft)}
.btn.google{background:#fff;color:#1f1f1f;border:1px solid #dadce0;
font-family:Roboto,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;font-weight:500}
.btn.google:hover{background:#f8f9fa;border-color:#c6c9cd}
@media (prefers-color-scheme:dark){.btn.google{background:#131314;color:#e3e3e3;border-color:#3c4043}
.btn.google:hover{background:#1c1c1e;border-color:#5f6368}}
.btn:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.caption{margin:0;padding:0 2px;font-size:12.5px;line-height:1.5;color:var(--faint)}
.caption-lead{display:block;color:var(--muted);font-weight:560;margin-bottom:2px}
.note{display:flex;gap:9px;margin:18px 0 0;padding:11px 12px;border-radius:10px;background:var(--soft);
font-size:13px;line-height:1.5;color:var(--muted)}
.note svg{flex:none;margin-top:2px;color:var(--faint)}
.warn{display:flex;gap:10px;background:var(--warn-bg);border:1px solid var(--warn-line);color:var(--warn-text);
border-radius:10px;padding:11px 12px;font-size:13px;line-height:1.5;margin:0 0 16px}
.warn svg{flex:none;margin-top:1px}
.footer{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:22px;padding-top:18px;
border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
.footer strong{color:var(--text);font-weight:560;overflow-wrap:anywhere}
.btn.secondary{height:32px;padding:0 12px;font-size:13px;font-weight:520;color:var(--text);
border:1px solid var(--line);border-radius:8px}
.btn.secondary:hover{background:var(--soft)}
.foot{text-align:center;color:var(--faint);font-size:12px;margin-top:18px;letter-spacing:.01em}
.card{animation:rise .5s cubic-bezier(.16,1,.3,1) both}
.card>*{animation:rise .55s cubic-bezier(.16,1,.3,1) both}
.card>:nth-child(2){animation-delay:.04s}.card>:nth-child(3){animation-delay:.08s}
.card>:nth-child(4){animation-delay:.12s}.card>:nth-child(5){animation-delay:.16s}
.card>:nth-child(6){animation-delay:.2s}.card>:nth-child(7){animation-delay:.24s}
.foot{animation:fade .6s .3s ease both}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes flow{0%{transform:translateX(-14px);opacity:0}20%{opacity:1}80%{opacity:1}100%{transform:translateX(40px);opacity:0}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
"""

_ICON_BRAND = (
    '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '
    'stroke-linecap="round" aria-hidden="true"><circle cx="6" cy="7" r="2.3"/><circle cx="18" cy="7" r="2.3"/>'
    '<circle cx="12" cy="17.5" r="2.3"/><path d="M8.2 7.6 15.8 7.6M7.2 9 10.8 15.5M16.8 9 13.2 15.5"/></svg>'
)
_ICON_APP = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linejoin="round" aria-hidden="true"><path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-6l-4 3v-3H5a2 2 0 0 '
    '1-2-2V7a2 2 0 0 1 2-2Z"/><path d="M8 10.5h8M8 13h5" stroke-linecap="round"/></svg>'
)
_ICON_INFO = (
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5h.01"/></svg>'
)
_ICON_WARN = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2 20h20L12 3Z"/>'
    '<path d="M12 10v4.5M12 17.5h.01"/></svg>'
)
_ICON_GITHUB = (
    '<svg width="20" height="20" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 '
    "0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94"
    "-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64"
    "-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2"
    " .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73"
    '.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>'
)
# Google's standard multicolour "G", as used on its sign-in buttons. Kept unaltered.
_ICON_GOOGLE = (
    '<svg width="20" height="20" viewBox="0 0 48 48" aria-hidden="true">'
    '<path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 '
    '2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>'
    '<path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 '
    '6c4.51-4.18 7.09-10.36 7.09-17.65z"/>'
    '<path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 '
    '0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>'
    '<path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57'
    '-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>'
)


def _render_page(title: str, card_html: str, status: int = 200) -> HTMLResponse:
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><meta name="color-scheme" content="light dark">
<title>{html.escape(title)} · CogniCore</title><style>{_PAGE_CSS}</style></head>
<body><main><div class="card">{card_html}</div>
<p class="foot">CogniCore Memory · Secure sign-in</p></main></body></html>"""
    return HTMLResponse(page, status_code=status, headers=_PAGE_HEADERS)


def _error_page(message: str, status: int = 400) -> HTMLResponse:
    return _render_page("Sign-in could not continue", f"""
<div class="marks"><div class="mark alert">{_ICON_WARN.replace('width="18" height="18"', 'width="20" height="20"')}</div></div>
<h1>Sign-in could not continue</h1>
<p class="lede">{html.escape(message)}</p>
<p class="caption">You can close this window.</p>""", status)


def _client_label(redirect_uri: str) -> tuple[str, str]:
    """(app name, where the user returns) for the consent screen."""
    target = urllib.parse.urlparse(redirect_uri)
    if target.hostname in LOOPBACK_HOSTS:
        if target.path.startswith("/cognicore-cli/"):  # `cognicore login` (cognicore.sync)
            return "CogniCore CLI", target.netloc
        return "Claude Code", target.netloc
    if redirect_uri in CLAUDE_REDIRECT_URIS:
        return "Claude", target.netloc
    if redirect_uri == CHATGPT_LEGACY_REDIRECT or CHATGPT_REDIRECT.match(redirect_uri):
        return "ChatGPT", target.netloc
    return target.hostname or "An application", target.netloc


def _http_json(url: str, data: dict[str, str] | None = None, token: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "CogniCore-MCP"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)  # nosec B310
    with urllib.request.urlopen(req, timeout=8) as resp:  # nosec B310
        return json.loads(resp.read().decode("utf-8"))


class GoogleSignInError(Exception):
    """A Google sign-in step failed. The message is a fixed reason, safe to log:
    it never contains a code, a token or the user's ID."""


def validate_google_id_token(id_token: str, *, client_id: str, nonce: str, jwks: jwt.PyJWKClient) -> str:
    """Verify a Google ID token completely and return its ``sub``.

    Raises GoogleSignInError on any failure. Only RS256 signatures from Google's
    published keys are accepted, so ``alg=none`` or a token signed with a shared
    secret cannot pass.
    """
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise GoogleSignInError("ID token is malformed") from None
    if header.get("alg") != "RS256":
        raise GoogleSignInError("ID token is not RS256-signed")
    try:
        key = jwks.get_signing_key_from_jwt(id_token).key
    except jwt.PyJWTError:
        raise GoogleSignInError("ID token signing key not found") from None
    try:
        claims = jwt.decode(
            id_token, key, algorithms=["RS256"], audience=client_id, issuer=GOOGLE_ISSUERS,
            leeway=GOOGLE_LEEWAY, options={"require": ["iss", "aud", "exp", "iat", "sub"]},
        )
    except jwt.PyJWTError:
        raise GoogleSignInError("ID token failed verification") from None
    # PyJWT accepts a list audience containing ours; Google issues a single one.
    if claims.get("aud") != client_id:
        raise GoogleSignInError("ID token audience mismatch")
    if "azp" in claims and claims["azp"] != client_id:
        raise GoogleSignInError("ID token authorized party mismatch")
    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not secrets.compare_digest(token_nonce, nonce):
        raise GoogleSignInError("ID token nonce mismatch")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise GoogleSignInError("ID token has no subject")
    return sub


class OAuthServer:
    """Stateless OAuth 2.1 authorization server backed by Google and GitHub sign-in."""

    def __init__(self, secret: str, algorithm: str = "HS256"):
        self._secret = secret
        self._algorithm = algorithm
        self._consumed: dict[str, float] = {}
        self._jwks_clients: dict[str, jwt.PyJWKClient] = {}

    def _google_jwks(self) -> jwt.PyJWKClient:
        """One cached key-set client per JWKS URL; it refetches when Google rotates keys."""
        url = _google_jwks_url()
        if url not in self._jwks_clients:
            self._jwks_clients[url] = jwt.PyJWKClient(
                url, cache_keys=True, lifespan=3600, timeout=8, headers={"User-Agent": "CogniCore-MCP"},
            )
        return self._jwks_clients[url]

    def _google_pkce_verifier(self, nonce: str) -> str:
        """PKCE verifier for a Google sign-in, derived on the server so it never
        appears in a URL. Only its S256 challenge is sent to the browser."""
        digest = hmac.new(self._secret.encode(), f"google-pkce:{nonce}".encode(), hashlib.sha256)
        return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")

    # -- signing ---------------------------------------------------------------

    def _sign(self, typ: str, ttl: int, **claims: Any) -> str:
        now = int(time.time())
        payload = {"typ": typ, "iat": now, "exp": now + ttl, **claims}
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def _verify(self, token: str, typ: str) -> dict[str, Any] | None:
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algorithm])
        except jwt.InvalidTokenError:
            return None
        return payload if payload.get("typ") == typ else None

    def _consume(self, jti: str, exp: float) -> bool:
        """Mark a one-time ID used. Returns False if it was already used."""
        now = time.time()
        for key in [k for k, v in self._consumed.items() if v < now]:
            del self._consumed[key]
        if jti in self._consumed:
            return False
        self._consumed[jti] = exp
        return True

    # -- discovery -------------------------------------------------------------

    def protected_resource_metadata(self, request: Request, resource_path: str) -> JSONResponse:
        base = base_url(request)
        return JSONResponse({
            "resource": f"{base}{resource_path}",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp"],
            "resource_name": "CogniCore Memory",
        })

    def authorization_server_metadata(self, request: Request) -> JSONResponse:
        base = base_url(request)
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": SCOPES,
            "authorization_response_iss_parameter_supported": True,
        })

    # -- dynamic client registration (RFC 7591) --------------------------------

    async def register(self, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        redirect_uris = body.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _oauth_error("invalid_redirect_uri", "redirect_uris is required")
        bad = [u for u in redirect_uris if not isinstance(u, str) or not redirect_uri_allowed(u)]
        if bad:
            return _oauth_error("invalid_redirect_uri", f"redirect URI not allowed: {bad[0]}")
        return JSONResponse({
            "client_id": f"cognicore_{secrets.token_urlsafe(16)}",
            "client_id_issued_at": int(time.time()),
            "client_name": body.get("client_name", "MCP client"),
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }, status_code=201)

    # -- authorization ---------------------------------------------------------

    def authorize(self, request: Request):
        q = request.query_params
        client_id = q.get("client_id", "")
        redirect_uri = q.get("redirect_uri", "")
        state = q.get("state")
        # Never redirect to an unvalidated URI: report these problems on our own page.
        if not client_id:
            return _error_page("The request is missing client_id.")
        if not redirect_uri_allowed(redirect_uri):
            return _error_page("The application asked to return to an address this server does not allow.")
        if q.get("response_type") != "code":
            return RedirectResponse(_with_query(redirect_uri, {"error": "unsupported_response_type", "state": state}), 302)
        challenge = q.get("code_challenge", "")
        if not challenge or q.get("code_challenge_method") != "S256":
            return RedirectResponse(_with_query(redirect_uri, {
                "error": "invalid_request",
                "error_description": "PKCE with code_challenge_method=S256 is required",
                "state": state,
            }), 302)

        auth_request = self._sign(
            "authreq", REQUEST_TTL,
            client_id=client_id, redirect_uri=redirect_uri, code_challenge=challenge,
            state=state, scope=q.get("scope", ""), resource=q.get("resource", ""),
        )
        app_name, return_to = _client_label(redirect_uri)
        loopback = urllib.parse.urlparse(redirect_uri).hostname in LOOPBACK_HOSTS
        warning = f"""<div class="warn" role="note">{_ICON_WARN}<div>Only continue if you just started this
connection from {html.escape(app_name)}. Any program on this computer can request a local sign-in.</div></div>""" if loopback else ""
        query = urllib.parse.urlencode({"request": auth_request})
        cancel_url = _with_query(redirect_uri, {
            "error": "access_denied", "error_description": "The user cancelled sign-in", "state": state,
        })
        name = html.escape(app_name)

        # (provider, what we receive, what we never receive, button). Google first:
        # it is the account most people already have. Each disclosure sits under
        # its own button, so it is read at the moment of choosing.
        providers = []
        if google_enabled():
            google_url = html.escape(f"/authorize/google?{query}")
            providers.append((
                "Google",
                "Only your Google account ID, used to keep your memories separate.",
                "No access to your email, contacts, Drive or any other Google data.",
                f'<a class="btn google" href="{google_url}">{_ICON_GOOGLE}Continue with Google</a>',
            ))
        if github_enabled():
            github_url = html.escape(f"/authorize/github?{query}")
            providers.append((
                "GitHub",
                "Your public GitHub username and account ID, used only to keep your memories separate.",
                "No access to your repositories, email or private data.",
                f'<a class="btn outline" href="{github_url}">{_ICON_GITHUB}Continue with GitHub</a>',
            ))
        # With one provider there is no button to pair the text with by eye, so name it.
        single = len(providers) == 1
        options = "\n".join(
            f"""<div class="option">{button}
<p class="caption">{f'<span class="caption-lead">CogniCore receives from {provider}</span>' if single else ""}{gets} {never}</p></div>"""
            for provider, gets, never, button in providers
        )
        # Signing in with the other provider opens an empty account, which looks like
        # lost memories. Say so where the choice is made.
        separate = "" if single else f"""<p class="note">{_ICON_INFO}<span>Google and GitHub are separate accounts.
Each sign-in option keeps its own memories, so choose the same one every time.</span></p>"""
        return _render_page(f"Connect {app_name}", f"""
<div class="marks"><div class="mark app" title="{name}">{_ICON_APP}</div>
<div class="link" aria-hidden="true"></div>
<div class="mark brand" title="CogniCore">{_ICON_BRAND}</div></div>
<h1>Connect {name} to CogniCore Memory</h1>
<p class="lede">{name} will be able to save and recall memories in your private CogniCore account.
No new account or password needed.</p>
{warning}<div class="actions">{options}</div>
{separate}
<div class="footer"><span>You&rsquo;ll return to <strong>{html.escape(return_to)}</strong></span>
<a class="btn secondary" href="{html.escape(cancel_url)}">Cancel</a></div>""")

    # -- shared by both providers ----------------------------------------------

    def _start_provider(self, request: Request):
        """Check the consent page's link. Returns (authreq, fresh nonce, None),
        or ("", "", error page) when the link is invalid or expired."""
        auth_request = request.query_params.get("request", "")
        if not self._verify(auth_request, "authreq"):
            return "", "", _error_page("This sign-in link has expired. Start the connection again from Claude.")
        return auth_request, secrets.token_urlsafe(16), None

    @staticmethod
    def _redirect_with_nonce(request: Request, url: str, nonce: str) -> RedirectResponse:
        """Send the browser to the provider, binding the sign-in to it with a cookie."""
        resp = RedirectResponse(url, 302)
        resp.set_cookie(
            NONCE_COOKIE, nonce, max_age=REQUEST_TTL, httponly=True, samesite="lax",
            secure=base_url(request).startswith("https://"), path="/oauth",
        )
        return resp

    def _check_callback(self, request: Request, state_typ: str):
        """Verify a provider callback's state and its typ, the browser's nonce cookie,
        and the original authorization request, in that order.

        Returns (state claims, authreq claims, None) or (None, None, error page).
        Failures here show our own page and never redirect: until the authreq is
        verified there is no validated redirect URI to send them to.
        """
        provider_state = self._verify(request.query_params.get("state", ""), state_typ)
        if not provider_state:
            return None, None, _error_page(
                "This sign-in has expired or was not started here. Start the connection again from Claude.")
        cookie = request.cookies.get(NONCE_COOKIE, "")
        if not cookie or not secrets.compare_digest(cookie, str(provider_state.get("nonce", ""))):
            return None, None, _error_page(
                "This sign-in was started in a different browser. Start the connection again from Claude.")
        auth_request = self._verify(provider_state.get("req", ""), "authreq")
        if not auth_request:
            return None, None, _error_page("This sign-in has expired. Start the connection again from Claude.")
        return provider_state, auth_request, None

    @staticmethod
    def _client_error(auth_request: dict[str, Any], error: str, description: str) -> RedirectResponse:
        """Report an error to the client's already-validated redirect URI, with its own state."""
        return RedirectResponse(_with_query(auth_request["redirect_uri"], {
            "error": error, "error_description": description, "state": auth_request.get("state"),
        }), 302)

    def _issue_code(self, request: Request, auth_request: dict[str, Any], sub: str, login: str) -> RedirectResponse:
        """Send the client back with our own single-use, PKCE-bound authorization code."""
        redirect_uri = auth_request["redirect_uri"]
        code = self._sign(
            "code", CODE_TTL, jti=secrets.token_urlsafe(16),
            sub=sub, login=login,
            client_id=auth_request["client_id"], redirect_uri=redirect_uri,
            code_challenge=auth_request["code_challenge"], scope=auth_request.get("scope", ""),
        )
        resp = RedirectResponse(_with_query(redirect_uri, {
            "code": code, "state": auth_request.get("state"), "iss": base_url(request),
        }), 302)
        resp.delete_cookie(NONCE_COOKIE, path="/oauth")
        return resp

    # -- GitHub ----------------------------------------------------------------

    def authorize_github(self, request: Request):
        auth_request, nonce, error = self._start_provider(request)
        if error:
            return error
        state = self._sign("ghstate", REQUEST_TTL, req=auth_request, nonce=nonce)
        url = f"{_github_oauth_base()}/login/oauth/authorize?" + urllib.parse.urlencode({
            "client_id": os.environ["GITHUB_CLIENT_ID"],
            "redirect_uri": f"{base_url(request)}{CALLBACK_PATH}",
            "state": state,
            "allow_signup": "true",
        })
        return self._redirect_with_nonce(request, url, nonce)

    async def github_callback(self, request: Request):
        q = request.query_params
        _, auth_request, error = self._check_callback(request, "ghstate")
        if error:
            return error

        if q.get("error") or not q.get("code"):
            return self._client_error(
                auth_request, "access_denied", q.get("error_description", "GitHub sign-in was cancelled"))

        try:
            token = await asyncio.to_thread(_http_json, f"{_github_oauth_base()}/login/oauth/access_token", {
                "client_id": os.environ["GITHUB_CLIENT_ID"],
                "client_secret": os.environ["GITHUB_CLIENT_SECRET"],
                "code": q["code"],
                "redirect_uri": f"{base_url(request)}{CALLBACK_PATH}",
            })
            gh_token = token.get("access_token")
            if not gh_token:
                raise ValueError(token.get("error_description") or token.get("error") or "no access_token")
            user = await asyncio.to_thread(_http_json, f"{_github_api_base()}/user", None, gh_token)
            github_id = user["id"]
        except (OSError, ValueError, KeyError) as exc:  # network, bad JSON, or unexpected GitHub response
            print(f"[OAUTH] GitHub sign-in failed: {exc}")
            return self._client_error(auth_request, "server_error", "GitHub sign-in failed")

        return self._issue_code(request, auth_request, f"github:{github_id}", user.get("login", ""))

    # -- Google (OpenID Connect, scope openid only) ----------------------------

    def authorize_google(self, request: Request):
        auth_request, nonce, error = self._start_provider(request)
        if error:
            return error
        # A typ of its own, so a GitHub state cannot be replayed here, or the reverse.
        state = self._sign("googlestate", REQUEST_TTL, req=auth_request, nonce=nonce)
        url = f"{_google_authorize_url()}?" + urllib.parse.urlencode({
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "redirect_uri": f"{base_url(request)}{GOOGLE_CALLBACK_PATH}",
            "response_type": "code",
            "scope": "openid",
            "prompt": "select_account",
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_s256(self._google_pkce_verifier(nonce)),
            "code_challenge_method": "S256",
        })
        return self._redirect_with_nonce(request, url, nonce)

    async def google_callback(self, request: Request):
        q = request.query_params
        google_state, auth_request, error = self._check_callback(request, "googlestate")
        if error:
            return error

        if q.get("error") or not q.get("code"):
            return self._client_error(auth_request, "access_denied", "Google sign-in was cancelled")

        nonce = google_state["nonce"]
        client_id = os.environ["GOOGLE_CLIENT_ID"]
        try:
            token = await asyncio.to_thread(_http_json, _google_token_url(), {
                "grant_type": "authorization_code",
                "code": q["code"],
                "client_id": client_id,
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": f"{base_url(request)}{GOOGLE_CALLBACK_PATH}",
                "code_verifier": self._google_pkce_verifier(nonce),
            })
            id_token = token.get("id_token") if isinstance(token, dict) else None
            if not isinstance(id_token, str) or not id_token:
                raise GoogleSignInError("token response has no ID token")
            # Fetching Google's signing keys can block, so this runs off the event loop too.
            google_sub = await asyncio.to_thread(
                validate_google_id_token, id_token, client_id=client_id, nonce=nonce, jwks=self._google_jwks(),
            )
        except GoogleSignInError as exc:  # a fixed reason, safe to log
            print(f"[OAUTH] Google sign-in failed: {exc}")
            return self._client_error(auth_request, "server_error", "Google sign-in failed")
        except (OSError, ValueError) as exc:  # network or bad JSON; the message may echo request data
            print(f"[OAUTH] Google sign-in failed: {type(exc).__name__} at the token endpoint")
            return self._client_error(auth_request, "server_error", "Google sign-in failed")
        # Google's access and ID tokens go out of scope here; nothing from them is stored.
        return self._issue_code(request, auth_request, f"google:{google_sub}", "")

    # -- token endpoint --------------------------------------------------------

    def _issue_tokens(self, sub: str, login: str, client_id: str, scope: str) -> JSONResponse:
        access = self._sign("access", ACCESS_TOKEN_TTL, sub=sub, login=login, client_id=client_id, scope=scope)
        refresh = self._sign(
            "refresh", REFRESH_TOKEN_TTL, jti=secrets.token_urlsafe(16),
            sub=sub, login=login, client_id=client_id, scope=scope,
        )
        return JSONResponse({
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "refresh_token": refresh,
            "scope": scope or "mcp",
        }, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    async def token(self, request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "")
        try:
            if "application/json" in content_type:
                form = await request.json()
            else:
                form = dict(await request.form())
        except (ValueError, MultiPartException):
            return _oauth_error("invalid_request", "could not parse request body")
        grant_type = form.get("grant_type", "")

        if grant_type == "authorization_code":
            code = self._verify(form.get("code", ""), "code")
            if not code:
                return _oauth_error("invalid_grant", "authorization code is invalid or expired")
            if form.get("redirect_uri") != code["redirect_uri"]:
                return _oauth_error("invalid_grant", "redirect_uri does not match the authorization request")
            if form.get("client_id") and form["client_id"] != code["client_id"]:
                return _oauth_error("invalid_grant", "client_id does not match the authorization request")
            verifier = form.get("code_verifier", "")
            if not verifier or not secrets.compare_digest(_pkce_s256(verifier), code["code_challenge"]):
                return _oauth_error("invalid_grant", "PKCE verification failed")
            if not self._consume(code["jti"], code["exp"]):
                return _oauth_error("invalid_grant", "authorization code was already used")
            return self._issue_tokens(code["sub"], code.get("login", ""), code["client_id"], code.get("scope", ""))

        if grant_type == "refresh_token":
            refresh = self._verify(form.get("refresh_token", ""), "refresh")
            if not refresh:
                return _oauth_error("invalid_grant", "refresh token is invalid or expired")
            if form.get("client_id") and form["client_id"] != refresh["client_id"]:
                return _oauth_error("invalid_grant", "client_id does not match the refresh token")
            if not self._consume(refresh["jti"], refresh["exp"]):
                return _oauth_error("invalid_grant", "refresh token was already used")
            return self._issue_tokens(refresh["sub"], refresh.get("login", ""), refresh["client_id"], refresh.get("scope", ""))

        return _oauth_error("unsupported_grant_type", f"grant_type {grant_type!r} is not supported")


# Backwards-compatible name from when GitHub was the only sign-in provider.
GitHubOAuthServer = OAuthServer

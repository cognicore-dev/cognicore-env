"""OAuth 2.1 authorization server for the remote MCP server, with GitHub sign-in.

Claude (claude.ai, Desktop, mobile, Claude Code) connects to a remote MCP server
through OAuth: it reads protected-resource metadata, registers itself with
Dynamic Client Registration, sends the user to ``/authorize`` with PKCE, and
exchanges the code at ``/token``. This module implements that server side and
delegates the actual sign-in to GitHub, so every user gets a stable, distinct
identity (``github:<numeric id>``) without running a user database.

Everything is stateless except a small in-memory set of consumed code and
refresh-token IDs. Requests, codes and tokens are JWTs signed with the server's
``COGNICORE_JWT_SECRET`` and told apart by their ``typ`` claim. The GitHub
access token is used once to read the user's ID and is never stored.

Enabled when ``GITHUB_CLIENT_ID`` and ``GITHUB_CLIENT_SECRET`` are set.
``COGNICORE_PUBLIC_URL`` should be set to the server's public origin
(e.g. ``https://mcp.example.com``) so the issuer is stable behind proxies.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
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

# MCP endpoints a user can enter as the connector URL. Protected-resource
# metadata must name the exact URL the user entered.
SSE_RESOURCE_PATH = "/mcp/sse"
STREAM_RESOURCE_PATH = "/mcp/stream"


def github_enabled() -> bool:
    return bool(os.environ.get("GITHUB_CLIENT_ID") and os.environ.get("GITHUB_CLIENT_SECRET"))


def _github_oauth_base() -> str:
    return os.environ.get("COGNICORE_GITHUB_OAUTH_BASE", "https://github.com").rstrip("/")


def _github_api_base() -> str:
    return os.environ.get("COGNICORE_GITHUB_API_BASE", "https://api.github.com").rstrip("/")


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
:root{--bg:#f6f7f9;--card:#fff;--text:#14161a;--muted:#5b616e;--line:#e4e6eb;--soft:#f3f4f6;
--brand:#4f46e5;--brand2:#7c3aed;--btn:#1f2328;--btn-text:#fff;--btn-hover:#32383f;
--ok:#16a34a;--no:#9ca3af;--warn-bg:#fff7e6;--warn-line:#f5d08a;--warn-text:#7a4b00;--shadow:0 1px 2px rgba(16,24,40,.04),0 12px 32px rgba(16,24,40,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#0d0f13;--card:#16191f;--text:#eceef2;--muted:#9aa1ad;--line:#2a2f38;
--soft:#1d2129;--btn:#f0f2f5;--btn-text:#14161a;--btn-hover:#d9dde3;--ok:#4ade80;--no:#6b7280;
--warn-bg:#2a2112;--warn-line:#5c4415;--warn-text:#f3c97a;--shadow:0 12px 32px rgba(0,0,0,.45)}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px 16px;
background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}
main{width:100%;max-width:440px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:32px 28px 24px;box-shadow:var(--shadow)}
.marks{display:flex;align-items:center;justify-content:center;gap:12px;margin-bottom:20px}
.mark{width:52px;height:52px;border-radius:14px;display:grid;place-items:center}
.mark.brand{background:linear-gradient(135deg,var(--brand),var(--brand2))}
.mark.app{background:var(--soft);border:1px solid var(--line);color:var(--muted)}
.mark.alert{background:var(--warn-bg);border:1px solid var(--warn-line);color:var(--warn-text)}
.link{display:flex;gap:4px}.link i{width:5px;height:5px;border-radius:50%;background:var(--line)}
h1{font-size:20px;line-height:1.3;font-weight:650;letter-spacing:-.01em;text-align:center;margin:0 0 6px}
.lede{color:var(--muted);text-align:center;margin:0 0 22px}
.section{border-top:1px solid var(--line);padding-top:16px;margin-top:16px}
.label{font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin:0 0 10px}
ul{list-style:none;margin:0;padding:0;display:grid;gap:10px}
li{display:flex;gap:10px;align-items:flex-start}
li svg{flex:none;margin-top:3px}
.ok{color:var(--ok)}.no{color:var(--no)}
.return{display:flex;align-items:center;justify-content:space-between;gap:12px;background:var(--soft);
border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:14px}
.return span{color:var(--muted)}.return strong{font-weight:600;overflow-wrap:anywhere;text-align:right}
.warn{display:flex;gap:10px;background:var(--warn-bg);border:1px solid var(--warn-line);color:var(--warn-text);
border-radius:10px;padding:12px;font-size:14px;margin-top:16px}
.warn svg{flex:none;margin-top:2px}
.actions{display:grid;gap:10px;margin-top:22px}
.btn{display:flex;align-items:center;justify-content:center;gap:10px;height:46px;border-radius:10px;font-weight:600;
font-size:15px;text-decoration:none;transition:background .15s,border-color .15s}
.btn.primary{background:var(--btn);color:var(--btn-text)}.btn.primary:hover{background:var(--btn-hover)}
.btn.secondary{color:var(--text);border:1px solid var(--line)}.btn.secondary:hover{background:var(--soft)}
.btn:focus-visible{outline:3px solid var(--brand);outline-offset:2px}
.foot{text-align:center;color:var(--muted);font-size:13px;margin-top:16px}
"""

_ICON_BRAND = (
    '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8" '
    'stroke-linecap="round" aria-hidden="true"><circle cx="6" cy="7" r="2.3"/><circle cx="18" cy="7" r="2.3"/>'
    '<circle cx="12" cy="17.5" r="2.3"/><path d="M8.2 7.6 15.8 7.6M7.2 9 10.8 15.5M16.8 9 13.2 15.5"/></svg>'
)
_ICON_APP = (
    '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linejoin="round" aria-hidden="true"><path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-6l-4 3v-3H5a2 2 0 0 '
    '1-2-2V7a2 2 0 0 1 2-2Z"/><path d="M8 10.5h8M8 13h5" stroke-linecap="round"/></svg>'
)
_ICON_CHECK = (
    '<svg class="ok" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3 8.5 3.2 3L13 4.5"/></svg>'
)
_ICON_CROSS = (
    '<svg class="no" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" aria-hidden="true"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg>'
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
<div class="marks"><div class="mark alert">{_ICON_WARN.replace('width="18" height="18"', 'width="26" height="26"')}</div></div>
<h1>Sign-in could not continue</h1>
<p class="lede">{html.escape(message)}</p>
<p class="foot" style="margin:0">You can close this window.</p>""", status)


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


class GitHubOAuthServer:
    """Stateless OAuth 2.1 authorization server backed by GitHub sign-in."""

    def __init__(self, secret: str, algorithm: str = "HS256"):
        self._secret = secret
        self._algorithm = algorithm
        self._consumed: dict[str, float] = {}

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
        continue_url = f"/authorize/github?{urllib.parse.urlencode({'request': auth_request})}"
        cancel_url = _with_query(redirect_uri, {
            "error": "access_denied", "error_description": "The user cancelled sign-in", "state": state,
        })
        name = html.escape(app_name)
        return _render_page(f"Connect {app_name}", f"""
<div class="marks"><div class="mark app" title="{name}">{_ICON_APP}</div>
<div class="link" aria-hidden="true"><i></i><i></i><i></i></div>
<div class="mark brand" title="CogniCore">{_ICON_BRAND}</div></div>
<h1>Connect {name} to CogniCore Memory</h1>
<p class="lede">{name} will be able to save and recall memories in your private CogniCore account.</p>
<div class="section"><p class="label">CogniCore receives from GitHub</p>
<ul><li>{_ICON_CHECK}<span>Your public GitHub username and account ID, used only to keep your memories separate</span></li>
<li>{_ICON_CROSS}<span>No access to your repositories, email or private data</span></li></ul></div>
<div class="section"><div class="return"><span>After sign-in you return to</span><strong>{html.escape(return_to)}</strong></div>
{warning}</div>
<div class="actions"><a class="btn primary" href="{html.escape(continue_url)}">{_ICON_GITHUB}Continue with GitHub</a>
<a class="btn secondary" href="{html.escape(cancel_url)}">Cancel</a></div>""")

    def authorize_github(self, request: Request):
        auth_request = request.query_params.get("request", "")
        if not self._verify(auth_request, "authreq"):
            return _error_page("This sign-in link has expired. Start the connection again from Claude.")
        nonce = secrets.token_urlsafe(16)
        state = self._sign("ghstate", REQUEST_TTL, req=auth_request, nonce=nonce)
        url = f"{_github_oauth_base()}/login/oauth/authorize?" + urllib.parse.urlencode({
            "client_id": os.environ["GITHUB_CLIENT_ID"],
            "redirect_uri": f"{base_url(request)}{CALLBACK_PATH}",
            "state": state,
            "allow_signup": "true",
        })
        resp = RedirectResponse(url, 302)
        resp.set_cookie(
            NONCE_COOKIE, nonce, max_age=REQUEST_TTL, httponly=True, samesite="lax",
            secure=base_url(request).startswith("https://"), path="/oauth",
        )
        return resp

    async def github_callback(self, request: Request):
        q = request.query_params
        gh_state = self._verify(q.get("state", ""), "ghstate")
        if not gh_state:
            return _error_page("This sign-in has expired or was not started here. Start the connection again from Claude.")
        cookie = request.cookies.get(NONCE_COOKIE, "")
        if not cookie or not secrets.compare_digest(cookie, gh_state.get("nonce", "")):
            return _error_page("This sign-in was started in a different browser. Start the connection again from Claude.")
        auth_request = self._verify(gh_state.get("req", ""), "authreq")
        if not auth_request:
            return _error_page("This sign-in has expired. Start the connection again from Claude.")
        redirect_uri, client_state = auth_request["redirect_uri"], auth_request.get("state")

        if q.get("error") or not q.get("code"):
            return RedirectResponse(_with_query(redirect_uri, {
                "error": "access_denied", "error_description": q.get("error_description", "GitHub sign-in was cancelled"),
                "state": client_state,
            }), 302)

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
            return RedirectResponse(_with_query(redirect_uri, {
                "error": "server_error", "error_description": "GitHub sign-in failed", "state": client_state,
            }), 302)

        code = self._sign(
            "code", CODE_TTL, jti=secrets.token_urlsafe(16),
            sub=f"github:{github_id}", login=user.get("login", ""),
            client_id=auth_request["client_id"], redirect_uri=redirect_uri,
            code_challenge=auth_request["code_challenge"], scope=auth_request.get("scope", ""),
        )
        resp = RedirectResponse(_with_query(redirect_uri, {
            "code": code, "state": client_state, "iss": base_url(request),
        }), 302)
        resp.delete_cookie(NONCE_COOKIE, path="/oauth")
        return resp

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

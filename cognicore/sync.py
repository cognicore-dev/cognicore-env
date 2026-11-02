"""
Optional sync between local memory and your hosted CogniCore account.

With sync on, Claude Code and Codex (local hooks) and claude.ai, Claude
Desktop and the mobile app (hosted connector) share one memory:

    cognicore login        sign in once (GitHub, in the browser)
    cognicore sync         push and pull now; prints what moved
    cognicore sync status  who is signed in, and when sync last ran
    cognicore logout       stop syncing and forget the sign-in

After a hook saves a session, a background process uploads that session's
handoff, which is already redacted and is never the raw transcript. At the start of a
session, the hook downloads sessions saved elsewhere since the last pull
(3-second timeout) into the local store. Nothing is sent anywhere until you
run ``cognicore login``.

Sign-in uses the hosted server's own OAuth 2.1 flow (dynamic client
registration, PKCE, loopback redirect, RFC 8252), the same one the Claude
connector uses. The refresh token is kept in ``sync.json`` in the data
directory, readable only by you. Standard library only.

Environment:
    COGNICORE_SYNC          "0" pauses sync without signing out
    COGNICORE_SYNC_SERVER   server for ``cognicore login`` (default the hosted one)
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_SERVER = "https://mcp.cognicoree.tech"
STATE_FILE = "sync.json"
CALLBACK_PATH = "/cognicore-cli/callback"  # the server's consent page names the CLI for this path
PULL_TIMEOUT = 3.0
PUSH_TIMEOUT = 15.0
PULL_OVERLAP = 300  # seconds re-read on each pull, against clock skew; imports are idempotent
PULL_LIMIT = 50


class SyncError(Exception):
    """Sync could not complete. The message is safe to show to the user."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    from cognicore.hooks import data_dir

    return data_dir()


def _state_path() -> Path:
    return _data_dir() / STATE_FILE


def load_state() -> Dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: Dict[str, Any]) -> None:
    """Write atomically, readable only by the owner (the file holds a refresh token)."""
    path = _state_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def enabled() -> bool:
    """Signed in, and not paused with COGNICORE_SYNC=0."""
    return os.environ.get("COGNICORE_SYNC") != "0" and bool(load_state().get("refresh_token"))


class _Lock:
    """A cross-process lock file. Refresh tokens are single use, so two hooks
    refreshing at once would sign the user out without it."""

    def __init__(self, name: str, wait: float = 10.0, stale: float = 30.0):
        self.path, self.wait, self.stale = _data_dir() / name, wait, stale

    def __enter__(self):
        deadline = time.monotonic() + self.wait
        while True:
            try:
                os.close(os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale:
                        self.path.unlink()
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise SyncError("another CogniCore process is holding the sync lock")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _request(method: str, url: str, body: Any = None, token: str = "", form: bool = False,
             timeout: float = PUSH_TIMEOUT) -> Dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "cognicore-sync"}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - https server from config
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read() or b"{}").get("error_description", "")
        except ValueError:
            pass
        raise _HTTPError(e.code, detail or e.reason) from None
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise SyncError(f"could not reach {urllib.parse.urlparse(url).netloc}: {getattr(e, 'reason', e)}") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise SyncError("the server sent a response that is not JSON") from None


class _HTTPError(SyncError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"server returned {status}: {detail}")
        self.status = status


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------

def _pkce() -> tuple:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _wait_for_callback(server: http.server.HTTPServer, timeout: float) -> Dict[str, str]:
    result: Dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = ("Signed in. CogniCore will now sync your memory. You can close this tab."
                   if ok else "Sign-in did not complete. You can close this tab and try again.")
            self.wfile.write(f"<!doctype html><title>CogniCore</title><p style='font:16px system-ui;"
                             f"margin:3em'>{msg}</p>".encode())

        def log_message(self, *args):  # keep the terminal quiet
            pass

    server.RequestHandlerClass = Handler
    server.timeout = 1.0
    deadline = time.monotonic() + timeout
    while not result and time.monotonic() < deadline:
        server.handle_request()
    return result


def login(server_url: str = "", open_browser: bool = True, timeout: float = 300.0, out=print) -> Dict[str, Any]:
    """Sign in to a CogniCore server in the browser and keep the tokens for sync."""
    base = (server_url or os.environ.get("COGNICORE_SYNC_SERVER") or DEFAULT_SERVER).rstrip("/")
    meta = _request("GET", f"{base}/.well-known/oauth-authorization-server", timeout=15)
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if not meta.get(key):
            raise SyncError(f"{base} does not offer sign-in ({key} missing)")

    httpd = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    try:
        redirect_uri = f"http://127.0.0.1:{httpd.server_address[1]}{CALLBACK_PATH}"
        client = _request("POST", meta["registration_endpoint"], {
            "client_name": "CogniCore CLI", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "token_endpoint_auth_method": "none",
        }, timeout=15)
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(16)
        url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
            "scope": "mcp offline_access", "resource": f"{base}/mcp/stream",
        })
        out(f"Opening your browser to sign in to {urllib.parse.urlparse(base).netloc}.")
        out(f"If it doesn't open, visit:\n  {url}")
        if open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        result = _wait_for_callback(httpd, timeout)
    finally:
        httpd.server_close()

    if not result:
        raise SyncError("sign-in timed out")
    if result.get("state") != state:
        raise SyncError("sign-in response did not match this request; try again")
    if "code" not in result:
        raise SyncError(f"sign-in failed: {result.get('error_description') or result.get('error', 'unknown error')}")
    tokens = _request("POST", meta["token_endpoint"], {
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect_uri,
        "client_id": client["client_id"], "code_verifier": verifier,
    }, form=True, timeout=15)
    state_doc = {
        "server": base, "token_endpoint": meta["token_endpoint"], "client_id": client["client_id"],
        "access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": time.time() + float(tokens.get("expires_in", 3600)),
        "login": _token_login(tokens["access_token"]), "signed_in_at": time.time(),
    }
    if not state_doc["refresh_token"]:
        raise SyncError("the server did not issue a refresh token, so sync could not stay signed in")
    save_state(state_doc)
    return state_doc


def _token_login(token: str) -> str:
    """The GitHub username in an access token, for display only (the server verifies tokens)."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return str(claims.get("login") or claims.get("sub") or "")
    except (IndexError, ValueError):
        return ""


def logout() -> bool:
    try:
        _state_path().unlink()
        return True
    except OSError:
        return False


def access_token() -> str:
    """A valid access token, refreshing it (under a lock) when it is about to expire."""
    state = load_state()
    if not state.get("refresh_token"):
        raise SyncError("not signed in; run `cognicore login`")
    if state.get("access_token") and state.get("expires_at", 0) - time.time() > 60:
        return state["access_token"]
    with _Lock("sync.lock"):
        state = load_state()  # another process may have refreshed while we waited
        if state.get("access_token") and state.get("expires_at", 0) - time.time() > 60:
            return state["access_token"]
        try:
            tokens = _request("POST", state["token_endpoint"], {
                "grant_type": "refresh_token", "refresh_token": state["refresh_token"],
                "client_id": state["client_id"],
            }, form=True, timeout=15)
        except _HTTPError as e:
            if e.status in (400, 401):
                raise SyncError("the sign-in has expired; run `cognicore login` again") from None
            raise
        state.update(access_token=tokens["access_token"],
                     refresh_token=tokens.get("refresh_token") or state["refresh_token"],
                     expires_at=time.time() + float(tokens.get("expires_in", 3600)))
        save_state(state)
        return state["access_token"]


# ---------------------------------------------------------------------------
# Push and pull
# ---------------------------------------------------------------------------

def push(backend: Any, external_id: str, timeout: float = PUSH_TIMEOUT) -> Dict[str, Any]:
    """Upload one local session's handoff. Its local save time is the revision."""
    with backend._get_conn() as conn:
        row = conn.execute("SELECT handoff_json, timestamp FROM sessions WHERE external_id = ?",
                           (external_id,)).fetchone()
    if not row or not row["handoff_json"]:
        return {"status": "nothing to push"}
    state = load_state()
    res = _request("PUT", f"{state['server']}/sync/sessions", {
        "external_id": external_id, "revision": row["timestamp"], "handoff": json.loads(row["handoff_json"]),
    }, token=access_token(), timeout=timeout)
    _touch("last_push")
    return res


def pull(backend: Any, timeout: float = PULL_TIMEOUT) -> List[Dict[str, Any]]:
    """Download sessions saved elsewhere since the last pull. Returns those that are new here."""
    from cognicore.memory.context_preservation import import_handoff

    state = load_state()
    since = max(0.0, float(state.get("pull_cursor", 0)) - PULL_OVERLAP)
    res = _request("GET", f"{state['server']}/sync/sessions?" + urllib.parse.urlencode(
        {"since": since, "limit": PULL_LIMIT}), token=access_token(), timeout=timeout)
    imported, cursor = [], float(state.get("pull_cursor", 0))
    for item in res.get("sessions") or []:
        try:
            revision = float(item["revision"])
            done = import_handoff(backend, item["handoff"], str(item["external_id"]), revision)
        except (KeyError, TypeError, ValueError):
            continue
        cursor = max(cursor, revision)
        if done.get("status") == "created":
            doc = item["handoff"]
            imported.append({
                "session_id": done["session_id"],
                "session_name": doc.get("provenance", {}).get("session_name", ""),
                "goal": doc.get("goal", ""),
                "client": doc.get("provenance", {}).get("client") or doc.get("provenance", {}).get("agent") or "",
                "project": doc.get("provenance", {}).get("project") or "",
            })
    _update_state(pull_cursor=cursor, last_pull=time.time())
    return imported


def _update_state(**changes: Any) -> None:
    """Read-modify-write under the lock, so a concurrent token refresh is never overwritten."""
    with _Lock("sync.lock"):
        state = load_state()
        if state.get("refresh_token"):
            state.update(changes)
            save_state(state)


def _touch(key: str) -> None:
    _update_state(**{key: time.time()})


def push_in_background(external_id: str) -> None:
    """Start ``python -m cognicore.sync push <id>`` detached, so a hook never waits on the network."""
    args = [sys.executable, "-m", "cognicore.sync", "push", external_id]
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)  # nosec B603 - fixed argv, no shell


def pointer_text(imported: List[Dict[str, Any]], project: str = "") -> str:
    """One short line per session that arrived from another app, for the start-of-session brief."""
    from cognicore.memory.context_preservation import project_key

    here = project_key(project) if project else ""
    lines = []
    for s in imported:
        if s["project"] and s["project"] == here:
            continue  # it's this project's own work; the brief already covers it
        where = s["client"] or "another app"
        what = s["goal"] or s["session_name"] or "a session"
        lines.append(f"- From {where}: {what[:160]} (cognicore_resume_session(session_id='{s['session_id']}'))")
    if not lines:
        return ""
    return "New from your other apps since last time:\n" + "\n".join(lines[:3])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def status_text() -> str:
    state = load_state()
    if not state.get("refresh_token"):
        return "Sync is off. Run `cognicore login` to share memory with claude.ai, Claude Desktop and mobile."
    paused = " (paused: COGNICORE_SYNC=0)" if os.environ.get("COGNICORE_SYNC") == "0" else ""
    return (f"Signed in to {state.get('server')} as {state.get('login') or 'unknown'}{paused}.\n"
            f"Last push: {_fmt(state.get('last_push'))}. Last pull: {_fmt(state.get('last_pull'))}.")


def sync_now(out=print) -> int:
    from cognicore.hooks import _backend

    backend = _backend()
    with backend._get_conn() as conn:
        keys = [r[0] for r in conn.execute(
            "SELECT external_id FROM sessions WHERE external_id IS NOT NULL AND external_id NOT LIKE 'hosted:%' "
            "AND handoff_json IS NOT NULL ORDER BY timestamp DESC LIMIT 20").fetchall()]
    pushed = 0
    for key in keys:
        res = push(backend, key)
        pushed += res.get("status") in ("created", "updated")
    imported = pull(backend, timeout=PUSH_TIMEOUT)
    out(f"Pushed {pushed} session(s), pulled {len(imported)} new session(s).")
    for s in imported:
        out(f"  + {s['session_name'] or s['session_id']}: {s['goal'][:100]}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "now"
    try:
        if command == "push" and len(argv) > 1:  # the background upload started by a hook
            if enabled():
                from cognicore.hooks import _backend

                push(_backend(), argv[1])
            return 0
        if command == "login":
            state = login(argv[1] if len(argv) > 1 else "")
            print(f"Signed in as {state['login'] or 'you'}. Claude Code and Codex now sync with your hosted memory.")
            return 0
        if command == "logout":
            print("Signed out; sync is off." if logout() else "Sync was already off.")
            return 0
        if command == "status":
            print(status_text())
            return 0
        if command == "now":
            if not load_state().get("refresh_token"):
                print(status_text())
                return 1
            return sync_now()
        print("usage: cognicore sync [now|status] | cognicore login [server] | cognicore logout")
        return 2
    except SyncError as e:
        if command == "push":
            from cognicore.hooks import _log

            _log(f"sync push failed: {e}")
            return 0
        print(f"Sync failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

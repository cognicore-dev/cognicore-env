"""Getting documents into a hosted account's memory, directly or through a one-time link.

Two ways in, both ending in ``cognicore.memory.documents.add_document`` on the signed-in
user's own database:

direct   ChatGPT passes a file the user attached to a tool that declares it in
         ``_meta["openai/fileParams"]``: the tool receives ``{download_url, file_id,
         mime_type?, file_name?}`` and the server downloads the file (``fetch_attached_file``).
         Only HTTPS URLs on ALLOWED_FILE_HOSTS that resolve to public addresses are fetched,
         redirects are not followed, and at most MAX_BYTES are read.

link     Any app: the tool returns a single-use link, valid LINK_TTL seconds, to a page where
         the user picks the file in a browser (``upload_page``). The link is
         ``<server>/documents/upload#t=<token>``: the token is in the fragment, which browsers
         never send to the server, so it never reaches access logs. The page sends the bytes
         with ``PUT /documents/upload?name=<file name>`` and the token in the UPLOAD_HEADER
         header; anything that can send such a request can use the token directly. The token
         is signed and names the user, so it can only add documents to that user's memory: it
         reads nothing, works once, and expires.

Settings (environment):
    COGNICORE_DOCUMENT_FILE_HOSTS  comma-separated host suffixes direct downloads may come from
                                   (default: oaiusercontent.com,openai.com)
"""

import ipaddress
import os
import secrets
import socket
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from cognicore.memory.documents import MAX_BYTES, DocumentError

LINK_TTL = 15 * 60
TOKEN_TYPE = "docupload"
UPLOAD_HEADER = "X-CogniCore-Upload-Token"
UPLOAD_PATH = "/documents/upload"
DEFAULT_FILE_HOSTS = ("oaiusercontent.com", "openai.com")
FETCH_TIMEOUT = 30.0
_EXTENSIONS = {"application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md", "image/png": ".png",
               "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def allowed_file_hosts() -> Tuple[str, ...]:
    raw = os.environ.get("COGNICORE_DOCUMENT_FILE_HOSTS", "")
    hosts = tuple(h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip())
    return hosts or DEFAULT_FILE_HOSTS


def _host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in allowed_file_hosts())


def _resolves_to_public(host: str) -> bool:
    """True only if every address the host resolves to is a public one."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def fetch_attached_file(file: Any, transport=None) -> Tuple[bytes, str]:
    """Download a file ChatGPT attached to a tool call. Returns (bytes, file name).

    *transport* is for tests (an ``httpx`` transport); production uses the network.
    """
    import httpx

    if not isinstance(file, dict) or not isinstance(file.get("download_url"), str):
        raise DocumentError("No file was attached.")
    url = file["download_url"]
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise DocumentError("The attached file's link is not an HTTPS link.")
    if not _host_allowed(parts.hostname):
        raise DocumentError("The attached file's link is not from a file host this server accepts.")
    if transport is None and not _resolves_to_public(parts.hostname):
        raise DocumentError("The attached file's link does not point to a public server.")

    name = os.path.basename(str(file.get("file_name") or "")) or "document"
    if "." not in name and file.get("mime_type") in _EXTENSIONS:
        name += _EXTENSIONS[file["mime_type"]]
    data = bytearray()
    try:
        with httpx.Client(transport=transport, timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise DocumentError(f"The attached file could not be downloaded (HTTP {resp.status_code}).")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    raise DocumentError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.")
                for chunk in resp.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise DocumentError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    except httpx.HTTPError as e:
        raise DocumentError(f"The attached file could not be downloaded ({type(e).__name__}).") from e
    return bytes(data), name


# --- one-time links -------------------------------------------------------------------------

def make_link_token(oauth_server, user_id: str) -> str:
    return oauth_server._sign(TOKEN_TYPE, LINK_TTL, sub=user_id, jti=secrets.token_urlsafe(16))


def check_link_token(oauth_server, token: str) -> Optional[Dict[str, Any]]:
    """The token's claims if it is a valid, unexpired upload link token, else None."""
    claims = oauth_server._verify(token, TOKEN_TYPE)
    if not claims or not isinstance(claims.get("sub"), str) or not claims.get("jti"):
        return None
    return claims


def use_link_token(oauth_server, token: str, used_db: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Like check_link_token, and marks the link used: a second use returns None.

    With *used_db*, used links are recorded in that SQLite file, so a link stays used across
    server restarts; without it, only in this process's memory.
    """
    claims = check_link_token(oauth_server, token)
    if not claims:
        return None
    if used_db is None:
        return claims if oauth_server._consume(claims["jti"], claims["exp"]) else None
    import sqlite3
    import time

    with sqlite3.connect(used_db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS used_upload_links (jti TEXT PRIMARY KEY, exp REAL NOT NULL)")
        conn.execute("DELETE FROM used_upload_links WHERE exp < ?", (time.time(),))
        try:
            conn.execute("INSERT INTO used_upload_links (jti, exp) VALUES (?, ?)", (claims["jti"], claims["exp"]))
        except sqlite3.IntegrityError:
            return None  # already used
    return claims


def link_for(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}{UPLOAD_PATH}#t={token}"


def upload_page() -> str:
    """The page behind a one-time link: pick a file, upload it, see the result.

    The token comes from the link's fragment and goes to the server only in a header."""
    max_mb = MAX_BYTES // (1024 * 1024)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Add a document to CogniCore</title>
<style>
:root {{ --bg:#f6f7f8; --fg:#16191d; --muted:#5b636d; --card:#fff; --line:#dde1e6; --accent:#0b6e72; --bad:#b3261e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111417; --fg:#e8ebee; --muted:#9aa3ad; --card:#181c20;
  --line:#2b3137; --accent:#4fc1c4; --bad:#f28b82; }} }}
body {{ background:var(--bg); color:var(--fg); font:16px/1.5 system-ui, sans-serif; margin:0; padding:48px 16px; }}
main {{ max-width:520px; margin:0 auto; background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:24px; display:grid; gap:14px; }}
h1 {{ font-size:22px; margin:0; }} p {{ margin:0; color:var(--muted); }}
button {{ background:var(--accent); color:#fff; border:0; border-radius:6px; padding:10px 16px; font-size:15px;
  cursor:pointer; }} button:disabled {{ opacity:.5; cursor:default; }}
#status {{ min-height:1.5em; color:var(--fg); }} #status.bad {{ color:var(--bad); }}
</style></head><body><main>
<h1>Add a document to your memory</h1>
<p>PDF, image, .txt or .md, up to {max_mb} MB. It is split into sections that your AI apps recall when a question
needs them. This link works once and expires 15 minutes after it was made.</p>
<input id="file" type="file"
  accept=".pdf,.txt,.md,.markdown,image/*,application/pdf,text/plain,text/markdown">
<button id="go" disabled>Upload</button>
<div id="status" role="status" aria-live="polite"></div>
</main>
<script>
const file = document.getElementById("file"), go = document.getElementById("go");
const status = document.getElementById("status");
const token = new URLSearchParams(location.hash.slice(1)).get("t") || "";
history.replaceState(null, "", location.pathname);  // keep the token out of history and bookmarks
function expired() {{
  try {{ const p = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
        return !p.exp || p.exp * 1000 < Date.now(); }} catch (e) {{ return true; }}
}}
if (expired()) {{ file.disabled = true; status.className = "bad";
  status.textContent = "This link has expired or is incomplete. Ask your AI app for a new upload link."; }}
file.addEventListener("change", () => {{ go.disabled = !file.files.length; }});
go.addEventListener("click", async () => {{
  const f = file.files[0]; if (!f) return;
  if (f.size > {MAX_BYTES}) {{
    status.className = "bad"; status.textContent = "That file is larger than {max_mb} MB."; return;
  }}
  go.disabled = true; file.disabled = true; status.className = ""; status.textContent = "Uploading…";
  try {{
    const r = await fetch("{UPLOAD_PATH}?name=" + encodeURIComponent(f.name),
                          {{ method: "PUT", body: f, headers: {{ "{UPLOAD_HEADER}": token }} }});
    const body = await r.json().catch(() => ({{}}));
    if (!r.ok) throw new Error(body.detail || ("Upload failed (" + r.status + ")"));
    status.textContent = body.already_present
      ? body.name + " was already in your memory."
      : "Added " + body.name + ": " + body.sections + " sections. You can close this page.";
  }} catch (e) {{ status.className = "bad"; status.textContent = e.message; }}
}});
</script></body></html>"""

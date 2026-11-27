"""Hosted document upload: ChatGPT attachments (direct) and one-time links (any app)."""

import asyncio
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from cognicore.connectors import document_upload as up
from cognicore.connectors import remote
from cognicore.memory import documents as docs

ALICE, BOB = "github:4242", "google:777"
NOTE = b"The office opens at nine.\n\nVisitors sign in at the front desk."


def _ctx(user_id=ALICE):
    token = jwt.encode({"sub": user_id, "typ": "access", "login": user_id, "exp": int(time.time()) + 600},
                       remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM)
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"}, query_params={})
    return SimpleNamespace(request_context=SimpleNamespace(request=request, meta=None))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")
    monkeypatch.setenv("COGNICORE_PUBLIC_URL", "https://mcp.example.com")
    monkeypatch.delenv("COGNICORE_DB_PATH", raising=False)
    monkeypatch.delenv("COGNICORE_DOCUMENT_FILE_HOSTS", raising=False)


@pytest.fixture
def client():
    return TestClient(remote.app)


def _link_token(user=ALICE):
    out = remote.cognicore_add_document(_ctx(user))
    link = out.split("Upload link (works once, expires in 15 minutes): ")[1].split()[0]
    parts = urlsplit(link)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "mcp.example.com", "/documents/upload")
    assert parts.query == "" and parts.fragment.startswith("t=")  # the token never goes in the path or query
    return parts.fragment[2:]


def _put(client, token, data=NOTE, name="office.txt"):
    return client.put(f"/documents/upload?name={name}", content=data, headers={up.UPLOAD_HEADER: token})


def _names(user):
    return [d["name"] for d in docs.list_documents(remote.get_backend(_ctx(user)))]


# --- one-time links -----------------------------------------------------------------------------

def test_link_upload_adds_to_that_account_only(client):
    r = _put(client, _link_token(ALICE))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "office.txt" and body["sections"] == 1 and not body["already_present"]
    assert r.headers["cache-control"] == "no-store"
    assert _names(ALICE) == ["office.txt"] and _names(BOB) == []
    hit = remote.get_backend(_ctx(ALICE)).search("visitors front desk", top_k=1)[0]
    assert hit.entry.category == "document:office.txt"


def test_link_works_once(client):
    token = _link_token()
    assert _put(client, token).status_code == 200
    again = _put(client, token, data=b"other text", name="other.txt")
    assert again.status_code == 401 and "already used" in again.json()["detail"]
    assert _names(ALICE) == ["office.txt"]


def test_forged_expired_and_wrong_type_tokens_are_refused(client):
    expired = remote.oauth_server._sign(up.TOKEN_TYPE, -5, sub=ALICE, jti="x1")
    access = jwt.encode({"sub": ALICE, "typ": "access", "exp": int(time.time()) + 600},
                        remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM)
    forged = jwt.encode({"sub": ALICE, "typ": up.TOKEN_TYPE, "jti": "x2", "exp": int(time.time()) + 600},
                        "not-the-server-secret", algorithm="HS256")
    for token in ("", "garbage", expired, access, forged):
        r = _put(client, token)
        assert r.status_code == 401, token
    assert _names(ALICE) == []


def test_too_large_keeps_the_link_and_bad_files_are_explained(client, monkeypatch):
    token = _link_token()
    monkeypatch.setattr(docs, "MAX_BYTES", 10)
    assert _put(client, token).status_code == 413
    monkeypatch.setattr(docs, "MAX_BYTES", 25 * 1024 * 1024)
    bad = _put(client, token, data=b"PK\x03\x04 zip", name="archive.zip")  # now the link is used
    assert bad.status_code == 400 and "unsupported" in bad.json()["detail"]


def test_document_limit_per_account(client, monkeypatch):
    monkeypatch.setattr(remote, "MAX_DOCUMENTS", 1)
    assert _put(client, _link_token()).status_code == 200
    r = _put(client, _link_token(), data=b"second", name="second.txt")
    assert r.status_code == 400 and "remove one first" in r.json()["detail"]


def test_upload_page_has_no_token_and_strict_headers(client):
    r = client.get("/documents/upload")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    assert up.UPLOAD_HEADER in r.text and "location.hash" in r.text


def test_local_mode_points_to_the_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("COGNICORE_DB_PATH", str(tmp_path / "local.db"))
    assert "cognicore docs add" in remote.cognicore_add_document(_ctx())


# --- ChatGPT attachments ------------------------------------------------------------------------

def _transport(status=200, body=NOTE, headers=None):
    return httpx.MockTransport(lambda request: httpx.Response(status, content=body, headers=headers or {}))


def test_fetch_attached_file_from_allowed_host():
    f = {"download_url": "https://files.oaiusercontent.com/file-abc?sig=1", "file_id": "file_abc",
         "file_name": "notes.txt", "mime_type": "text/plain"}
    assert up.fetch_attached_file(f, transport=_transport()) == (NOTE, "notes.txt")


@pytest.mark.parametrize("file,message", [
    (None, "No file was attached"),
    ({"file_id": "file_abc"}, "No file was attached"),
    ({"download_url": "http://files.oaiusercontent.com/x"}, "not an HTTPS link"),
    ({"download_url": "https://evil.example.com/x"}, "not from a file host"),
    ({"download_url": "https://oaiusercontent.com.evil.com/x"}, "not from a file host"),
])
def test_fetch_attached_file_refuses_unsafe_links(file, message):
    with pytest.raises(docs.DocumentError, match=message):
        up.fetch_attached_file(file, transport=_transport())


def test_fetch_attached_file_does_not_follow_redirects_or_read_too_much(monkeypatch):
    f = {"download_url": "https://files.oaiusercontent.com/x", "file_id": "f"}
    with pytest.raises(docs.DocumentError, match="HTTP 302"):
        up.fetch_attached_file(f, transport=_transport(302, b"", {"location": "http://169.254.169.254/"}))
    monkeypatch.setattr(up, "MAX_BYTES", 5)
    with pytest.raises(docs.DocumentError, match="larger than"):
        up.fetch_attached_file(f, transport=_transport())


def test_private_addresses_are_not_fetched(monkeypatch):
    assert not up._resolves_to_public("localhost")
    monkeypatch.setenv("COGNICORE_DOCUMENT_FILE_HOSTS", "localhost")  # even an allowed host must be public
    with pytest.raises(docs.DocumentError, match="not point to a public server"):
        up.fetch_attached_file({"download_url": "https://localhost/x", "file_id": "f"})


def test_tool_adds_attached_file(monkeypatch):
    monkeypatch.setattr(up, "fetch_attached_file", lambda file: (NOTE, "notes.txt"))
    out = remote.cognicore_add_document(_ctx(), file={"download_url": "https://files.oaiusercontent.com/x"})
    assert out.startswith("Added notes.txt") and _names(ALICE) == ["notes.txt"] and _names(BOB) == []
    again = remote.cognicore_add_document(_ctx(), file={"download_url": "https://files.oaiusercontent.com/x"})
    assert "already in memory" in again


def test_tool_reports_download_errors(monkeypatch):
    def boom(file):
        raise docs.DocumentError("The attached file could not be downloaded (HTTP 403).")
    monkeypatch.setattr(up, "fetch_attached_file", boom)
    assert remote.cognicore_add_document(_ctx(), file={"x": 1}).startswith("Error: The attached file")


def test_tool_declares_its_file_parameter_for_chatgpt():
    tool = {t.name: t for t in asyncio.run(remote.mcp.list_tools())}["cognicore_add_document"]
    meta = getattr(tool, "meta", None) or getattr(tool, "_meta", None) or {}
    assert meta.get("openai/fileParams") == ["file"]
    assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint is False


# --- list and remove ----------------------------------------------------------------------------

def test_list_and_remove_are_per_account(client):
    _put(client, _link_token(ALICE))
    listing = remote.cognicore_list_documents(_ctx(ALICE))
    doc_id = listing.split()[0]
    assert "office.txt" in listing
    assert remote.cognicore_list_documents(_ctx(BOB)) == "No documents in memory."
    assert remote.cognicore_remove_document(doc_id, _ctx(BOB)) == "Not found"  # Bob cannot remove Alice's
    assert _names(ALICE) == ["office.txt"]
    assert remote.cognicore_remove_document(doc_id, _ctx(ALICE)).startswith("Removed")
    assert _names(ALICE) == []

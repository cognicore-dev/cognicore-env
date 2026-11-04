"""An empty account says which sign-in it belongs to.

Google and GitHub sign-ins are separate accounts that are never linked. Someone
who reconnects with the other provider lands in an empty account, which looks
exactly like lost memories, so the read tools say what happened.
"""
import time
from types import SimpleNamespace

import jwt
import pytest

from cognicore.connectors import remote

GOOGLE_USER, GITHUB_USER = "google:109876543210", "github:1001"


def _ctx(user_id):
    token = jwt.encode(
        {"sub": user_id, "typ": "access", "exp": int(time.time()) + 600},
        remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM,
    )
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"}, query_params={})
    return SimpleNamespace(request_context=SimpleNamespace(request=request, meta=None))


@pytest.fixture(autouse=True)
def hosted_server(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")
    monkeypatch.delenv("COGNICORE_DB_PATH", raising=False)
    for name in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.setenv(name, "configured")


@pytest.mark.parametrize("call", [
    lambda ctx: remote.cognicore_recall("deployment checklist", ctx),
    lambda ctx: remote.cognicore_list(ctx),
    lambda ctx: remote.cognicore_stats(ctx),
    lambda ctx: remote.cognicore_resume_session(ctx),
], ids=["recall", "list", "stats", "resume"])
def test_an_empty_account_names_its_sign_in_and_the_other_one(call):
    out = call(_ctx(GOOGLE_USER))
    assert "no memories yet" in out
    assert "your Google sign-in" in out
    assert "choose GitHub" in out


def test_it_names_google_for_a_github_account():
    out = remote.cognicore_recall("anything", _ctx(GITHUB_USER))
    assert "your GitHub sign-in" in out and "choose Google" in out


def test_no_note_once_the_account_holds_anything():
    """A search that misses in a used account is a normal miss, not a wrong account."""
    remote.cognicore_remember("We deploy on Fridays", _ctx(GOOGLE_USER))
    assert "no memories yet" not in remote.cognicore_recall("kubernetes quota", _ctx(GOOGLE_USER))
    assert "no memories yet" not in remote.cognicore_stats(_ctx(GOOGLE_USER))


def test_no_note_when_the_other_provider_is_not_offered(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID")
    assert "no memories yet" not in remote.cognicore_recall("anything", _ctx(GITHUB_USER))


def test_no_note_for_local_use():
    """Without a signed-in request there is no provider to name."""
    assert "no memories yet" not in remote.cognicore_recall("anything", None)


def test_json_resume_gets_no_appended_prose():
    """A caller may parse format="json", so nothing is added to it."""
    out = remote.cognicore_resume_session(_ctx(GOOGLE_USER), format="json")
    assert "no memories yet" not in out


def test_the_note_never_reveals_another_account():
    """It names providers, never an id, and reads nothing outside the caller's account."""
    remote.cognicore_remember("GitHub-side secret", _ctx(GITHUB_USER))
    out = remote.cognicore_recall("secret", _ctx(GOOGLE_USER))
    assert "GitHub-side secret" not in out
    assert "1001" not in out and "109876543210" not in out

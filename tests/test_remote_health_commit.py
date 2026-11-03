"""/health reports the running commit so anyone can tell what is live."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from cognicore.connectors import remote

SHA = "0698651" + "a" * 33


@pytest.fixture
def client():
    return TestClient(remote.app)


def test_reports_the_commit_the_image_was_built_from(client, monkeypatch):
    monkeypatch.setenv("COGNICORE_COMMIT", SHA)
    assert client.get("/health").json() == {"status": "ok", "version": "1.0", "commit": SHA}


def test_reports_unknown_without_a_commit(client, monkeypatch):
    monkeypatch.delenv("COGNICORE_COMMIT", raising=False)
    assert client.get("/health").json()["commit"] == "unknown"


@pytest.mark.parametrize("value", ["unknown", "<script>", "abc", "--upload-pack=x", SHA.upper(), SHA + "0"])
def test_only_a_hex_sha_is_ever_reported(client, monkeypatch, value):
    """The value ends up in other tools; nothing but a commit SHA may come out."""
    monkeypatch.setenv("COGNICORE_COMMIT", value)
    assert client.get("/health").json()["commit"] == "unknown"


def test_health_stays_exempt_from_rate_limiting():
    assert "/health" in remote.RateLimitMiddleware.EXEMPT_PATHS

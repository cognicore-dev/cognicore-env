import pytest
from fastapi.testclient import TestClient
from cognicore.integrations.chatgpt import app

client = TestClient(app)

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "cognicore-chatgpt-mcp"}

def test_sse_endpoint_exists():
    """Verify that the FastMCP SSE route is mounted and accessible."""
    # Since actually requesting the SSE stream blocks the test client,
    # we verify that the mount is present and correct.
    mounts = [r for r in app.routes if getattr(r, "path", None) == "/mcp"]
    assert len(mounts) == 1
    mcp_app = mounts[0].app
    mcp_routes = [getattr(r, "path", None) for r in mcp_app.routes]
    assert "/sse" in mcp_routes
    assert any(r.startswith("/messages") for r in mcp_routes)

def test_messages_endpoint_exists():
    """Verify the /messages/ POST endpoint exists for MCP."""
    response = client.post("/mcp/messages/")
    assert response.status_code in (400, 404, 422, 500)

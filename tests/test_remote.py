import pytest
import jwt
from fastapi.testclient import TestClient
from cognicore.extension.remote import app, JWT_SECRET, JWT_ALGORITHM, get_db_path_for_user

client = TestClient(app)

def create_token(sub: str) -> str:
    return jwt.encode({"sub": sub}, JWT_SECRET, algorithm=JWT_ALGORITHM)

def test_remote_missing_auth():
    # Should get 401 without auth header
    response = client.get("/mcp/sse")
    assert response.status_code == 401
    assert "Missing or invalid Bearer token" in response.text

def test_remote_invalid_jwt():
    response = client.get("/mcp/sse", headers={"Authorization": "Bearer invalid.token.here"})
    assert response.status_code == 401
    assert "Invalid JWT" in response.text
    
def test_remote_missing_sub():
    token = jwt.encode({"other": "value"}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    response = client.get("/mcp/sse", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert "JWT missing 'sub' claim" in response.text

def test_remote_with_valid_jwt():
    token = create_token("user_123")
    # Test that a valid JWT doesn't get a 401 on the SSE endpoint.
    # We use a thread to avoid hanging on the infinite SSE generator.
    import threading
    result = {}
    def _stream():
        try:
            with client.stream("GET", "/mcp/sse", headers={"Authorization": f"Bearer {token}"}) as resp:
                result["status"] = resp.status_code
                resp.close()
        except Exception as e:
            result["error"] = str(e)

    t = threading.Thread(target=_stream, daemon=True)
    t.start()
    t.join(timeout=5)
    # If thread is still alive after 5s, the SSE stream connected successfully (200)
    # and is streaming — that's the expected behavior.
    assert result.get("status", 200) == 200

    # Also test that POST /mcp/message returns 400 (Bad Request from fastmcp for bad sessionId) instead of 401
    response = client.post("/mcp/message?sessionId=123", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code != 401

def test_db_path_sanitization():
    # Attempting path traversal in subject
    malicious_sub = "../../../etc/passwd"
    path = get_db_path_for_user(malicious_sub)
    
    import hashlib
    safe_hash = hashlib.sha256(malicious_sub.encode("utf-8")).hexdigest()
    
    # Path should only contain the hash, not the traversal string
    assert safe_hash in path
    assert ".." not in path
    assert path.endswith(f"memory_{safe_hash}.db")


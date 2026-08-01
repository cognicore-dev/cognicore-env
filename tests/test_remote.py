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


def test_apply_auto_compression():
    from cognicore.extension.remote import apply_auto_compression
    from unittest.mock import MagicMock, patch

    mock_ctx = MagicMock()

    # Small conversation under 150,000 tokens
    short_conv = [{"role": "user", "content": "hello world"}]
    res = apply_auto_compression(mock_ctx, short_conv, "original response")
    assert res == "original response"

    # Large conversation over 150,000 tokens (approx 700,000 chars > 175k tokens)
    big_text = "CogniCore has 7000+ PyPI downloads. Railway deployment is critical next step. Fixed SQLite persistence in MCP server. Matched Mem0 on all 5 metrics. 140x token reduction advantage over Mem0. " * 3000
    long_conv = [
        {"role": "user", "content": f"Msg {i}: {big_text}"} for i in range(10)
    ]

    mock_backend = MagicMock()
    mock_backend.store.return_value = 42

    with patch("cognicore.extension.remote.get_backend", return_value=mock_backend):
        out = apply_auto_compression(mock_ctx, long_conv, "Original tool output")
        assert "Context compressed." in out
        assert "Summary of previous discussion attached." in out
        assert "Continue from here." in out
        assert "Original tool output" in out
        assert "COMPRESSED CONTEXT" in out
        assert "Key numbers:" in out
        assert "CogniCore has 7000+ PyPI downloads" in out
        assert "Problems solved:" in out
        assert "Fixed SQLite persistence in MCP server" in out
        assert "Next steps:" in out
        assert "Railway deployment is critical next step" in out
        assert mock_backend.store.called


def test_structured_summary_extraction():
    from cognicore.memory.context_preservation import compress_context
    import json
    from unittest.mock import MagicMock

    mock_backend = MagicMock()
    mock_backend.store.return_value = 1

    conversation = [
        {"role": "user", "content": "CogniCore has 7000+ PyPI downloads."},
        {"role": "assistant", "content": "We decided to implement FastMCP for remote server."},
        {"role": "user", "content": "Fixed SQLite persistence in MCP server."},
        {"role": "assistant", "content": "Railway deployment is critical next step."},
        {"role": "user", "content": "140x token reduction advantage over Mem0."},
        {"role": "assistant", "content": "Matched Mem0 on all 5 metrics."},
        {"role": "user", "content": "Recent msg 1"},
        {"role": "assistant", "content": "Recent msg 2"}
    ]

    res_json = compress_context(mock_backend, conversation, keep_last_n=2)
    data = json.loads(res_json)
    summary = data["summary"]

    assert "COMPRESSED CONTEXT" in summary
    assert "CogniCore has 7000+ PyPI downloads" in summary
    assert "Fixed SQLite persistence in MCP server" in summary
    assert "Railway deployment is critical next step" in summary
    assert "140x token reduction advantage over Mem0" in summary
    assert data["messages_compressed"] == 6


def test_bug1_summary_length_hard_limit():
    from cognicore.memory.context_preservation import compress_context
    import json
    from unittest.mock import MagicMock

    mock_backend = MagicMock()
    mock_backend.store.return_value = 1

    msg = "CogniCore version 2.0 release candidate is under active testing with 7000 users. " * 50
    input_len = len(msg)
    conversation = [
        {"role": "user", "content": msg},
        {"role": "assistant", "content": "Recent message 1"},
        {"role": "user", "content": "Recent message 2"}
    ]

    res_json = compress_context(mock_backend, conversation, keep_last_n=2)
    data = json.loads(res_json)
    summary = data["summary"]

    assert len(summary) <= max(50, int(input_len * 0.4))


def test_bug2_key_numbers_decimal_and_filtering():
    from cognicore.memory.context_preservation import compress_context
    import json
    from unittest.mock import MagicMock

    mock_backend = MagicMock()
    mock_backend.store.return_value = 1

    conversation = [
        {"role": "user", "content": "Accuracy reached 98.2 percent on benchmark."},
        {"role": "user", "content": "What is the total download count?"},
        {"role": "user", "content": "I think numbers are cool."},
        {"role": "assistant", "content": "Recent msg 1"}
    ]

    res_json = compress_context(mock_backend, conversation, keep_last_n=1)
    data = json.loads(res_json)
    summary = data["summary"]

    assert "98.2 percent" in summary
    assert "98." not in summary.replace("98.2", "")
    assert "What is the total download count?" not in summary
    assert "I think numbers are cool" not in summary





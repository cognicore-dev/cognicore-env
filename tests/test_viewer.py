"""The local session viewer renders sessions and never writes to the database."""
import hashlib
import json

from cognicore.memory import SQLiteMemoryBackend
from cognicore.memory.context_preservation import save_session
from cognicore.memory.viewer import render_index, render_session


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_viewer_lists_and_shows_sessions_read_only(tmp_path):
    db = tmp_path / "mcp_memories.db"
    b = SQLiteMemoryBackend(str(db))
    sid = json.loads(save_session(b, [{"role": "user", "content": "Our goal is to fix <b>401s</b> on /api/v2/orders. "
                                                                 "TODO: add a test in tests/test_auth.py."}],
                                  session_name="jwt", project=str(tmp_path / "app")))["session_id"]
    before = _digest(db)

    index = render_index(db).decode()
    assert "jwt" in index and "app" in index and "1 open tasks" in index
    assert "<b>401s</b>" not in index and "&lt;b&gt;401s&lt;/b&gt;" in index  # escaped

    page = render_session(db, sid).decode()
    assert "tests/test_auth.py" in page and "Resume brief" in page and "Stored handoff" in page
    assert "No such session" in render_session(db, "sess_missing").decode()
    assert _digest(db) == before

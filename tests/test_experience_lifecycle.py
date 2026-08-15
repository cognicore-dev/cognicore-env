import pytest
import time
from cognicore.memory.base import MemoryEntry, MemoryState, MemoryScope, MemoryType, ExperiencePayload
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
from cognicore.memory.tfidf_embedder import TFIDFEmbeddingProvider

def test_experience_payload():
    # Test that the experience payload dictionaries work well with MemoryEntry metadata
    payload: ExperiencePayload = {
        "problem": "Cannot connect to database",
        "attempts": [
            {"approach": "Retry logic", "result": "Timeout", "reason": "Connection dropped"}
        ],
        "successful_approach": "Use connection pooling",
        "verification": {
            "command": "ping db",
            "result": "success",
            "result_hash": "abc1234",
            "exit_code": 0
        },
        "environment": {
            "python_version": "3.11",
            "dependencies": "sqlite3",
            "os_info": "windows"
        },
        "repository": {
            "commit": "HEAD",
            "paths": ["main.py"],
            "repo_id": "cognicore"
        }
    }
    
    entry = MemoryEntry(
        text="Database connection fix",
        memory_type=MemoryType.EXPERIENCE,
        state=MemoryState.UNVERIFIED,
        metadata={"experience": payload}
    )
    
    assert entry.metadata["experience"]["problem"] == "Cannot connect to database"
    assert entry.memory_state == MemoryState.UNVERIFIED

def test_promotion_lifecycle(tmp_path):
    db_path = tmp_path / "test_memory.db"
    backend = SQLiteMemoryBackend(str(db_path))
    
    entry = MemoryEntry(
        text="A test memory for promotion",
        state=MemoryState.UNVERIFIED,
        memory_type=MemoryType.EXPERIENCE
    )
    entry_id = backend.store(entry)
    
    # Update to OBSERVED
    backend.update(entry_id, state=MemoryState.OBSERVED.value)
    loaded = backend.get_by_id(entry_id)
    assert loaded.state == MemoryState.OBSERVED.value
    
    # Update to VERIFIED
    backend.update(entry_id, state=MemoryState.VERIFIED.value)
    loaded = backend.get_by_id(entry_id)
    assert loaded.state == MemoryState.VERIFIED.value
    
    # Update to PROMOTED
    backend.update(entry_id, state=MemoryState.PROMOTED.value)
    loaded = backend.get_by_id(entry_id)
    assert loaded.state == MemoryState.PROMOTED.value

def test_supersession(tmp_path):
    db_path = tmp_path / "test_memory.db"
    backend = SQLiteMemoryBackend(str(db_path))
    
    old_entry = MemoryEntry(text="Old API usage")
    old_id = backend.store(old_entry)
    
    new_entry = MemoryEntry(text="New API usage", supersedes=old_id)
    new_id = backend.store(new_entry)
    
    backend.update(old_id, invalidated_by=new_id, invalidated_reason="API deprecated")
    
    loaded_old = backend.get_by_id(old_id)
    assert loaded_old.invalidated_by == new_id
    assert loaded_old.invalidated_reason == "API deprecated"
    
    loaded_new = backend.get_by_id(new_id)
    assert loaded_new.supersedes == old_id

def test_metadata_filtering(tmp_path):
    db_path = tmp_path / "test_memory.db"
    backend = SQLiteMemoryBackend(str(db_path))
    
    backend.store(MemoryEntry(text="Python 3.9 bug", metadata={"python": "3.9"}))
    backend.store(MemoryEntry(text="Python 3.10 feature", metadata={"python": "3.10"}))
    backend.store(MemoryEntry(text="Python 3.11 feature", metadata={"python": "3.11"}))
    
    # Search without filter
    results = backend.search("feature")
    assert len(results) == 2
    
    # Search with filter
    results_311 = backend.search("feature", metadata_filters={"python": "3.11"})
    assert len(results_311) == 1
    assert results_311[0].entry.text == "Python 3.11 feature"
    
    results_39 = backend.search("bug", metadata_filters={"python": "3.9"})
    assert len(results_39) == 1
    assert results_39[0].entry.text == "Python 3.9 bug"

def test_tfidf_not_implemented():
    provider = TFIDFEmbeddingProvider()
    with pytest.raises(NotImplementedError):
        provider.embed("Test text")
        
    with pytest.raises(NotImplementedError):
        provider.embed_batch(["Test text 1", "Test text 2"])

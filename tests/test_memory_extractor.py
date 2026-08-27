import pytest
from unittest.mock import patch, MagicMock
from cognicore.memory.extractor import TranscriptExtractor, SYSTEM_PROMPT
from cognicore.memory.base import MemoryBackend, MemoryEntry, MemoryScope

class DummyBackend(MemoryBackend):
    def __init__(self):
        self.entries = []
    def store(self, entry: MemoryEntry) -> str:
        self.entries.append(entry)
        return f"mem_{len(self.entries)}"
    def retrieve(self, query: str, limit: int = 5) -> list:
        return []
    def update(self, entry_id: str, new_entry: MemoryEntry) -> bool:
        return True
    def delete(self, entry_id: str) -> bool:
        return True
    def clear(self) -> bool:
        self.entries = []
        return True
    def count(self) -> int:
        return len(self.entries)
    def get_by_category(self, category: str) -> list:
        return []
    def search(self, query: str, filters: dict = None) -> list:
        return []

def test_system_prompt_v2_keys():
    """Ensure the system prompt correctly specifies standard JSON keys"""
    assert "facts, rules, preferences, constraints, successful solutions, and failed approaches" in SYSTEM_PROMPT
    assert "\"text\"" in SYSTEM_PROMPT
    assert "\"memory_type\"" in SYSTEM_PROMPT
    assert "preference|semantic|constraint" in SYSTEM_PROMPT

@patch('cognicore.memory.extractor.ask_llm')
def test_extract_and_store_success(mock_ask):
    # Mock the LLM returning standard CogniCore memory format
    mock_ask.return_value = """
    ```json
    [
      {"text": "User prefers React", "memory_type": "preference"},
      {"text": "Applied mutex lock fix for deadlock", "memory_type": "semantic"}
    ]
    ```
    """
    
    backend = DummyBackend()
    extractor = TranscriptExtractor(backend)
    
    transcript = "User: I prefer React.\nAgent: Ok.\nUser: I applied the mutex lock fix for the deadlock."
    saved_ids = extractor.extract_and_store(transcript)
    
    # Verify the compression logic worked before sending to LLM
    args, kwargs = mock_ask.call_args
    assert "U: I prefer React." in kwargs['prompt']
    assert "A: Ok." in kwargs['prompt']
    
    # Verify the backend saved correctly
    assert len(saved_ids) == 2
    assert len(backend.entries) == 2
    
    assert backend.entries[0].text == "User prefers React"
    assert backend.entries[0].memory_type == "preference"
    assert backend.entries[0].scope == MemoryScope.AGENT
    
    assert backend.entries[1].text == "Applied mutex lock fix for deadlock"
    assert backend.entries[1].memory_type == "semantic"

@patch('cognicore.memory.extractor.ask_llm')
def test_extract_and_store_empty(mock_ask):
    mock_ask.return_value = "[]"
    backend = DummyBackend()
    extractor = TranscriptExtractor(backend)
    
    saved_ids = extractor.extract_and_store("User: Hi.\nAgent: Hello.")
    assert len(saved_ids) == 0
    assert len(backend.entries) == 0

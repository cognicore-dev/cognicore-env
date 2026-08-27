import json
import logging
import time
from typing import Optional, List

from cognicore.llm.gemini import ask_llm
from cognicore.memory.base import MemoryBackend, MemoryEntry, MemoryScope

logger = logging.getLogger("cognicore.memory.extractor")

SYSTEM_PROMPT = """Extract core facts, rules, preferences, constraints, successful solutions, and failed approaches from this transcript into a JSON array: [{"text": "3rd person detail", "memory_type": "preference|semantic|constraint"}]. Return [] if none. Preserve context and negations. ONLY output valid JSON."""

class TranscriptExtractor:
    """
    Automated Memory Extraction Pipeline.
    Listens to conversational transcripts, extracts facts via LLM,
    and intelligently stores them in the provided MemoryBackend.
    """
    
    def __init__(self, backend: MemoryBackend):
        self.backend = backend
        
    def extract_and_store(self, transcript: str, agent_id: str = "extractor_agent", scope: MemoryScope = MemoryScope.AGENT) -> List[str]:
        """
        Extracts memories from a transcript and routes them through the core MemoryBackend.
        Returns a list of entry_ids that were created.
        """
        logger.info("Extracting memories from transcript...")
        
        # Compress transcript safely
        lines = []
        for line in transcript.split("\n"):
            line = line.strip()
            if not line: continue
            if line.startswith("User:"):
                line = "U:" + line[len("User:"):]
            elif line.startswith("Agent:"):
                line = "A:" + line[len("Agent:"):]
            lines.append(line)
        compressed_transcript = "\n".join(lines)
        
        try:
            response = ask_llm(prompt=compressed_transcript, system=SYSTEM_PROMPT, max_tokens=500, temperature=0.1)
        except Exception as e:
            logger.error(f"LLM Extraction failed: {e}")
            return []
            
        try:
            clean_json = response.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.startswith("```"):
                clean_json = clean_json[3:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
                
            memories = json.loads(clean_json.strip())
        except Exception as e:
            logger.error(f"Failed to parse LLM JSON response: {response}")
            return []
            
        if not memories:
            logger.info("No memories extracted.")
            return []
            
        saved_ids = []
        for m in memories:
            text = m.get("text")
            mtype = m.get("memory_type", "semantic")
            if not text:
                continue
                
            entry = MemoryEntry(
                text=text,
                memory_type=mtype,
                scope=scope,
                scope_id=agent_id,
                state="active",
                timestamp=time.time()
            )
            
            entry_id = self.backend.store(entry)
            if entry_id:
                saved_ids.append(entry_id)
                
        logger.info(f"Successfully extracted and stored {len(saved_ids)} memories into backend.")
        return saved_ids

# Backwards compatibility function
def extract_memories(transcript: str, agent_id: str = "extractor_agent") -> List[dict]:
    """Legacy helper. Use TranscriptExtractor instead."""
    from cognicore.memory.chroma_backend import ChromaMemoryBackend
    backend = ChromaMemoryBackend()
    extractor = TranscriptExtractor(backend)
    extractor.extract_and_store(transcript, agent_id)
    return []

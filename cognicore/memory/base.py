"""Core data models and abstract interfaces for the CogniCore memory system.

This module defines the canonical data structures used throughout the memory
operating system: MemoryEntry, SearchResult, MemoryState, MemoryType, and
the abstract MemoryBackend interface that all storage backends must implement.

Version 2.0 — Agent Memory Operating System
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, TypedDict, Union
from dataclasses import dataclass, field
from enum import Enum

# ======================================================================
# Experience Schemas (Stored in MemoryEntry.metadata["experience"])
# ======================================================================

class FailedApproach(TypedDict, total=False):
    approach: str
    result: str
    reason: str

class VerificationEvidence(TypedDict, total=False):
    command: str
    result: str
    result_hash: str
    exit_code: int

class EnvironmentMetadata(TypedDict, total=False):
    python_version: str
    dependencies: str
    os_info: str

class RepositoryMetadata(TypedDict, total=False):
    commit: str
    paths: List[str]
    repo_id: str

class ExperiencePayload(TypedDict, total=False):
    problem: str
    attempts: List[FailedApproach]
    successful_approach: str
    verification: VerificationEvidence
    environment: EnvironmentMetadata
    repository: RepositoryMetadata

# ======================================================================
# Enums
# ======================================================================

class MemoryScope(Enum):
    """Memory isolation scopes for separating context."""
    GLOBAL = "global"
    USER = "user"
    SESSION = "session"
    AGENT = "agent"
    PROJECT = "project"
    REPOSITORY = "repository"
    ENVIRONMENT = "environment"


class MemoryState(str, Enum):
    """Lifecycle state of a memory entry.

    Memories progress through states based on actual usefulness:
        CANDIDATE → ACTIVE → VERIFIED → ARCHIVED → DELETED
    """
    UNVERIFIED = "unverified"     # Unverified experience
    OBSERVED = "observed"         # Agent asserted success/failure
    VERIFIED = "verified"         # Independent verification attached
    PROMOTED = "promoted"         # Approved for broader use
    TRANSFERABLE = "transferable" # Ready for cross-agent/cross-repo transfer
    
    # Legacy / Utility states
    CANDIDATE = "candidate"
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class MemoryType(str, Enum):
    """Classification of memory content.

    Each type has independent storage strategies, retrieval weights,
    scoring policies, and decay rates configured via TypePolicy.
    """
    SEMANTIC = "semantic"        # Facts, knowledge, definitions
    EPISODIC = "episodic"        # Events, conversations, experiences
    PROCEDURAL = "procedural"    # How-to, successful procedures
    PREFERENCE = "preference"    # User preferences, style choices
    CONSTRAINT = "constraint"    # Rules, restrictions, boundaries
    FAILURE = "failure"          # Mistakes, anti-patterns
    REFLECTION = "reflection"    # Meta-observations, insights
    KNOWLEDGE = "knowledge"      # Structured domain knowledge
    EXPERIENCE = "experience"    # Validated transferable experience

    @classmethod
    def from_string(cls, value: str) -> MemoryType:
        """Convert a string to MemoryType, with fallback to SEMANTIC."""
        try:
            return cls(value)
        except ValueError:
            # Backward compat: map old "fact" type to SEMANTIC
            _legacy_map = {"fact": cls.SEMANTIC, "general": cls.SEMANTIC}
            return _legacy_map.get(value, cls.SEMANTIC)


# ======================================================================
# Core Data Models
# ======================================================================

@dataclass
class MemoryEntry:
    """Canonical memory entry throughout the CogniCore system.

    All fields added in v2 have defaults so that existing code and
    serialized data continue to work without modification.
    """
    # --- Original fields (unchanged) ---
    text: str
    category: str = "general"
    correct: Optional[bool] = None
    action: str = ""
    scope: MemoryScope = MemoryScope.GLOBAL
    scope_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # --- Temporal fields (unchanged) ---
    session_id: str = "default"
    sequence_id: int = 0
    memory_type: str = "semantic"
    supersedes: Optional[str] = None
    invalidated_by: Optional[str] = None
    invalidated_reason: str = ""
    confidence: float = 1.0

    # --- Auto-populated by backend ---
    entry_id: str = ""
    timestamp: float = 0.0
    relevance: float = 1.0

    # --- NEW v2: Lifecycle fields ---
    state: str = "candidate"
    importance: float = 0.5
    creation_reason: str = ""
    source_component: str = ""
    source_agent: str = ""
    source_task: str = ""

    # --- NEW v2: Utility tracking ---
    retrieval_count: int = 0
    used_count: int = 0
    ignored_count: int = 0
    positive_outcomes: int = 0
    negative_outcomes: int = 0
    utility_score: float = 0.0
    last_accessed: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "category": self.category,
            "correct": self.correct,
            "action": self.action,
            "scope": self.scope.value if isinstance(self.scope, MemoryScope) else self.scope,
            "scope_id": self.scope_id,
            "metadata": self.metadata,
            "session_id": self.session_id,
            "sequence_id": self.sequence_id,
            "memory_type": self.memory_type,
            "supersedes": self.supersedes,
            "invalidated_by": self.invalidated_by,
            "invalidated_reason": self.invalidated_reason,
            "confidence": self.confidence,
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "relevance": self.relevance,
            # v2 fields
            "state": self.state,
            "importance": self.importance,
            "creation_reason": self.creation_reason,
            "source_component": self.source_component,
            "source_agent": self.source_agent,
            "source_task": self.source_task,
            "retrieval_count": self.retrieval_count,
            "used_count": self.used_count,
            "ignored_count": self.ignored_count,
            "positive_outcomes": self.positive_outcomes,
            "negative_outcomes": self.negative_outcomes,
            "utility_score": self.utility_score,
            "last_accessed": self.last_accessed,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> MemoryEntry:
        """Deserialize from dict, gracefully handling missing/legacy keys."""
        scope_str = d.get("scope", "global")
        try:
            scope = MemoryScope(scope_str)
        except ValueError:
            scope = MemoryScope.GLOBAL

        # Backward compat: map old "fact" memory_type to "semantic"
        raw_type = d.get("memory_type", "semantic")
        if raw_type == "fact":
            raw_type = "semantic"

        return cls(
            text=d.get("text", d.get("prompt", "")),
            category=d.get("category", "general"),
            correct=d.get("correct"),
            action=d.get("action", d.get("predicted", "")),
            scope=scope,
            scope_id=d.get("scope_id", ""),
            metadata=d.get("metadata", {}),
            session_id=d.get("session_id", "default"),
            sequence_id=d.get("sequence_id", 0),
            memory_type=raw_type,
            supersedes=d.get("supersedes"),
            invalidated_by=d.get("invalidated_by"),
            invalidated_reason=d.get("invalidated_reason", ""),
            confidence=d.get("confidence", 1.0),
            entry_id=d.get("entry_id", ""),
            timestamp=d.get("timestamp", d.get("_timestamp", 0.0)),
            relevance=d.get("relevance", d.get("_relevance", 1.0)),
            # v2 fields — all with safe defaults
            state=d.get("state", "candidate"),
            importance=d.get("importance", 0.5),
            creation_reason=d.get("creation_reason", ""),
            source_component=d.get("source_component", ""),
            source_agent=d.get("source_agent", ""),
            source_task=d.get("source_task", ""),
            retrieval_count=d.get("retrieval_count", 0),
            used_count=d.get("used_count", 0),
            ignored_count=d.get("ignored_count", 0),
            positive_outcomes=d.get("positive_outcomes", 0),
            negative_outcomes=d.get("negative_outcomes", 0),
            utility_score=d.get("utility_score", 0.0),
            last_accessed=d.get("last_accessed", 0.0),
        )

    @property
    def memory_state(self) -> MemoryState:
        """Return state as a MemoryState enum."""
        try:
            return MemoryState(self.state)
        except ValueError:
            return MemoryState.CANDIDATE

    @property
    def typed_memory_type(self) -> MemoryType:
        """Return memory_type as a MemoryType enum."""
        return MemoryType.from_string(self.memory_type)

    @property
    def is_searchable(self) -> bool:
        """Whether this entry should appear in search results."""
        return self.state not in (MemoryState.ARCHIVED.value, MemoryState.DELETED.value)


@dataclass
class SearchResult:
    """Standardized search result with score and provenance."""
    entry: MemoryEntry
    score: float
    source: str = ""  # "semantic", "category", "graph", "temporal", "embedding", "cross-encoder", "multihop", "sqlite"


# ======================================================================
# Retrieval Planning
# ======================================================================

@dataclass
class RetrievalPlan:
    """Output of the RetrievalPlanner — describes what to retrieve."""
    should_retrieve: bool
    memory_types: List[str] = field(default_factory=list)
    budget: int = 5
    priority_order: List[str] = field(default_factory=list)
    reasoning: str = ""


# ======================================================================
# Memory Tracing
# ======================================================================

@dataclass
class MemoryTrace:
    """Complete trace of a retrieval operation for debugging."""
    trace_id: str = ""
    query: str = ""
    plan: Optional[RetrievalPlan] = None
    retrieved: List[SearchResult] = field(default_factory=list)
    injected: List[SearchResult] = field(default_factory=list)
    outcome: Optional[str] = None           # "success" / "failure" / "unknown"
    task_success_delta: float = 0.0
    timestamp: float = 0.0


# ======================================================================
# Evaluation
# ======================================================================

@dataclass
class EvaluationResult:
    """Output of the MemoryEvaluator — decides if something should be stored."""
    should_store: bool
    memory_type: str = "semantic"
    importance: float = 0.5
    creation_reason: str = ""
    confidence: float = 1.0


# ======================================================================
# Abstract Backend Interface
# ======================================================================

class MemoryBackend(ABC):
    """Abstract interface for all memory storage backends.

    All backends must implement the core CRUD operations. The v2 lifecycle
    methods (update, get_by_state, get_by_type) have default implementations
    so existing backends continue to work without changes.
    """

    @abstractmethod
    def store(self, entry: MemoryEntry) -> str:
        """Store an entry. Returns entry_id."""
        pass

    @abstractmethod
    def search(self, query: str, top_k: int = 5,
               category: Optional[str] = None,
               scope: Optional[MemoryScope] = None,
               scope_id: Optional[str] = None,
               question_timestamp: Optional[float] = None,
               metadata_filters: Optional[Dict[str, str]] = None) -> List[SearchResult]:
        """Semantic + category search. Returns ranked results."""
        pass

    @abstractmethod
    def get_by_category(self, category: str, top_k: int = 5,
                        success_filter: Optional[bool] = None) -> List[MemoryEntry]:
        """Category-exact retrieval with optional success filter."""
        pass

    @abstractmethod
    def count(self) -> int:
        """Total entries in this backend."""
        pass

    @abstractmethod
    def clear(self) -> None:
        """Delete all entries."""
        pass

    def save(self) -> None:
        """Persist to disk (no-op for always-persistent backends like SQLite)."""
        pass

    def load(self) -> None:
        """Load from disk (no-op for always-persistent backends)."""
        pass

    def stats(self) -> Dict[str, Any]:
        """Return runtime statistics for this backend."""
        return {"count": self.count()}

    # ------------------------------------------------------------------
    # v2 Lifecycle methods (default implementations for backward compat)
    # ------------------------------------------------------------------

    def update(self, entry_id: str, **fields: Any) -> bool:
        """Update fields on an existing entry. Returns True if found.

        Default implementation does a linear scan. Backends should override
        this with something more efficient (e.g. SQL UPDATE).
        """
        for entry in self._get_all_entries():
            if entry.entry_id == entry_id:
                for key, value in fields.items():
                    if hasattr(entry, key):
                        setattr(entry, key, value)
                return True
        return False

    def get_by_id(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve a single entry by its ID."""
        for entry in self._get_all_entries():
            if entry.entry_id == entry_id:
                return entry
        return None

    def get_by_state(self, state: str, limit: int = 100) -> List[MemoryEntry]:
        """Retrieve entries in a given lifecycle state."""
        results = []
        for entry in self._get_all_entries():
            if entry.state == state:
                results.append(entry)
                if len(results) >= limit:
                    break
        return results

    def get_by_type(self, memory_type: str, limit: int = 100) -> List[MemoryEntry]:
        """Retrieve entries of a given memory type."""
        results = []
        for entry in self._get_all_entries():
            if entry.memory_type == memory_type:
                results.append(entry)
                if len(results) >= limit:
                    break
        return results

    def get_all(self, limit: int = 10000) -> List[MemoryEntry]:
        """Retrieve all entries (up to limit). Used by lifecycle manager."""
        return self._get_all_entries()[:limit]

    def delete(self, entry_id: str) -> bool:
        """Hard-delete an entry. Returns True if found and deleted.

        Default implementation does nothing — backends should override.
        """
        return False

    def _get_all_entries(self) -> List[MemoryEntry]:
        """Internal accessor for the entry list. Override for non-list backends."""
        if hasattr(self, "entries"):
            return self.entries
        return []


# ======================================================================
# Embedding Provider Interface (unchanged)
# ======================================================================

class EmbeddingProvider(ABC):
    """Abstract interface for embedding computation."""

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        """Compute embedding vector for text."""
        pass

    @abstractmethod
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Batch embedding computation."""
        pass

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Embedding vector dimension."""
        pass

"""
CogniCore Structured Experience Schema.

Defines the first-class StructuredExperience model that wraps MemoryEntry
for validated, transferable agent experience. Serializes into standard
MemoryEntry for storage in any existing backend.

No new database tables. No LLM dependency. Fully deterministic.
"""
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from cognicore.memory.base import (
    MemoryEntry,
    MemoryState,
    MemoryType,
)

logger = logging.getLogger("cognicore.experience")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AttemptOutcome(str, Enum):
    """Outcome of a single approach attempt."""
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class VerificationStatus(str, Enum):
    """Lifecycle state of experience verification.

    CANDIDATE  — recorded but not verified
    OBSERVED   — agent asserted outcome (not independently verified)
    VERIFIED   — independent evidence attached and validated
    PROMOTED   — approved for broader use
    TRANSFERABLE — verified + env match + provenance intact + no conflict
    INVALID    — failed re-validation or superseded
    SUPERSEDED — replaced by a newer experience
    """
    CANDIDATE = "candidate"
    OBSERVED = "observed"
    VERIFIED = "verified"
    PROMOTED = "promoted"
    TRANSFERABLE = "transferable"
    INVALID = "invalid"
    SUPERSEDED = "superseded"

    def to_memory_state(self) -> str:
        """Map to the existing MemoryState value strings."""
        _map = {
            self.CANDIDATE: MemoryState.CANDIDATE.value,
            self.OBSERVED: MemoryState.OBSERVED.value,
            self.VERIFIED: MemoryState.VERIFIED.value,
            self.PROMOTED: MemoryState.PROMOTED.value,
            self.TRANSFERABLE: MemoryState.TRANSFERABLE.value,
            self.INVALID: MemoryState.ARCHIVED.value,
            self.SUPERSEDED: MemoryState.ARCHIVED.value,
        }
        return _map.get(self, MemoryState.CANDIDATE.value)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Attempt:
    """A single approach attempt within an experience."""
    approach: str
    outcome: str = AttemptOutcome.UNKNOWN.value  # "success" | "failure" | "unknown"
    reason: str = ""
    evidence: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "approach": self.approach,
            "outcome": self.outcome,
            "reason": self.reason,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Attempt":
        return cls(
            approach=d.get("approach", ""),
            outcome=d.get("outcome", AttemptOutcome.UNKNOWN.value),
            reason=d.get("reason", ""),
            evidence=d.get("evidence", ""),
        )


@dataclass
class EvidenceRecord:
    """A single piece of verification evidence.

    Evidence is collected by the runtime/agent and passed to the
    VerificationGate for validation. The gate does NOT execute commands.
    """
    command: str = ""
    exit_code: int = 0
    stdout_hash: str = ""
    timestamp: str = ""
    commit: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout_hash": self.stdout_hash,
            "timestamp": self.timestamp,
            "commit": self.commit,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EvidenceRecord":
        return cls(
            command=d.get("command", ""),
            exit_code=int(d.get("exit_code", 0) or 0),
            stdout_hash=d.get("stdout_hash", ""),
            timestamp=d.get("timestamp", ""),
            commit=d.get("commit", ""),
        )


@dataclass
class RepositoryContext:
    """Identity of the repository/project where the experience was created."""
    repo_id: str = ""
    commit: str = ""
    branch: str = ""
    affected_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "commit": self.commit,
            "branch": self.branch,
            "affected_files": list(self.affected_files),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RepositoryContext":
        return cls(
            repo_id=d.get("repo_id", ""),
            commit=d.get("commit", ""),
            branch=d.get("branch", ""),
            affected_files=list(d.get("affected_files") or []),
        )


@dataclass
class EnvironmentContext:
    """Runtime environment metadata for compatibility checks."""
    python_version: str = ""
    os: str = ""
    framework: str = ""
    framework_version: str = ""
    dependencies: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "python_version": self.python_version,
            "os": self.os,
            "framework": self.framework,
            "framework_version": self.framework_version,
            "dependencies": dict(self.dependencies),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EnvironmentContext":
        return cls(
            python_version=d.get("python_version", ""),
            os=d.get("os", ""),
            framework=d.get("framework", ""),
            framework_version=d.get("framework_version", ""),
            dependencies=dict(d.get("dependencies") or {}),
        )


# ---------------------------------------------------------------------------
# Core experience model
# ---------------------------------------------------------------------------

@dataclass
class StructuredExperience:
    """A validated, transferable agent experience.

    This is the primary API object. It wraps a MemoryEntry for storage but
    provides a rich, validated schema on top. No new database tables are
    needed — the experience serializes into ``MemoryEntry.metadata["experience"]``.

    Lifecycle::

        CANDIDATE → OBSERVED → VERIFIED → PROMOTED → TRANSFERABLE

    TRANSFERABLE is stricter than VERIFIED:
        verified + env match + provenance intact + no conflict + not superseded.
    """
    # --- Identity ---
    experience_id: str = ""
    task: str = ""
    problem: str = ""

    # --- Attempts ---
    attempts: List[Attempt] = field(default_factory=list)

    # --- Solution ---
    solution: str = ""
    why_it_worked: str = ""

    # --- Verification ---
    verification_status: str = VerificationStatus.CANDIDATE.value
    verification_method: str = ""
    verification_version: str = "1"
    verification_evidence: List[EvidenceRecord] = field(default_factory=list)

    # --- Provenance ---
    source_agent: str = ""
    source_session: str = ""
    created_at: str = ""

    # --- Context ---
    repository: RepositoryContext = field(default_factory=RepositoryContext)
    environment: EnvironmentContext = field(default_factory=EnvironmentContext)

    # --- Scoring ---
    confidence: float = 0.5

    # --- Integrity ---
    content_hash: str = ""

    # --- Supersession ---
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.experience_id:
            self.experience_id = f"exp_{uuid.uuid4().hex[:12]}"
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.content_hash:
            self.content_hash = self.compute_content_hash()

    # ------------------------------------------------------------------
    # Content hash — deterministic provenance
    # ------------------------------------------------------------------

    def compute_content_hash(self) -> str:
        """Compute deterministic SHA-256 over immutable experience fields.

        Covers: task, problem, attempts, solution, verification_evidence,
        verification_method, verification_version, repository, environment.

        Excludes: confidence, state, superseded_by (these change over lifecycle).
        """
        canonical = json.dumps(
            {
                "task": self.task,
                "problem": self.problem,
                "attempts": [a.to_dict() for a in self.attempts],
                "solution": self.solution,
                "verification_evidence": [e.to_dict() for e in self.verification_evidence],
                "verification_method": self.verification_method,
                "verification_version": self.verification_version,
                "repository": self.repository.to_dict(),
                "environment": self.environment.to_dict(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def verify_hash(self) -> bool:
        """Recompute and compare content hash. Returns False if tampered."""
        return self.content_hash == self.compute_content_hash()

    # ------------------------------------------------------------------
    # MemoryEntry serialization
    # ------------------------------------------------------------------

    def to_memory_entry(self) -> MemoryEntry:
        """Serialize into a standard MemoryEntry for backend storage.

        Stores the full structured experience in ``metadata["experience"]``
        and maps lifecycle fields to existing MemoryEntry columns.
        """
        # Build the text field — a human-readable summary for FTS5 indexing
        text_parts = [f"Task: {self.task}"]
        if self.problem:
            text_parts.append(f"Problem: {self.problem}")
        for a in self.attempts:
            label = "✓" if a.outcome == AttemptOutcome.SUCCESS.value else "✗"
            text_parts.append(f"{label} {a.approach}: {a.reason}")
        if self.solution:
            text_parts.append(f"Solution: {self.solution}")
        if self.why_it_worked:
            text_parts.append(f"Why: {self.why_it_worked}")

        vs = VerificationStatus(self.verification_status)

        return MemoryEntry(
            entry_id=self.experience_id,
            text="\n".join(text_parts),
            category=self.task,
            memory_type=MemoryType.EXPERIENCE.value,
            state=vs.to_memory_state(),
            confidence=self.confidence,
            source_agent=self.source_agent,
            source_task=self.task,
            creation_reason="structured_experience",
            session_id=self.source_session or "default",
            supersedes=self.supersedes,
            invalidated_by=self.superseded_by,
            metadata={
                "experience": self._to_payload_dict(),
                "content_hash": self.content_hash,
            },
        )

    def to_failure_entries(self) -> List[MemoryEntry]:
        """Create separate FAILURE MemoryEntry objects for each failed attempt.

        Failures are stored as ``state="observed"`` — they are useful without
        formal verification but are clearly labeled as unverified observations.
        """
        entries: List[MemoryEntry] = []
        for attempt in self.attempts:
            if attempt.outcome != AttemptOutcome.FAILURE.value:
                continue
            text = (
                f"FAILURE: {attempt.approach}\n"
                f"Reason: {attempt.reason}\n"
                f"Task: {self.task}\n"
                f"Problem: {self.problem}"
            )
            entry = MemoryEntry(
                text=text,
                category=self.task,
                memory_type=MemoryType.FAILURE.value,
                state=MemoryState.OBSERVED.value,
                confidence=0.7,
                source_agent=self.source_agent,
                source_task=self.task,
                creation_reason="observed_failure",
                session_id=self.source_session or "default",
                metadata={
                    "parent_experience_id": self.experience_id,
                    "approach": attempt.approach,
                    "reason": attempt.reason,
                    "repository": self.repository.to_dict(),
                    "environment": self.environment.to_dict(),
                },
            )
            entries.append(entry)
        return entries

    @classmethod
    def from_memory_entry(cls, entry: MemoryEntry) -> "StructuredExperience":
        """Deserialize from a stored MemoryEntry."""
        metadata = entry.metadata or {}
        payload = metadata.get("experience", {})
        if not payload:
            # Not a structured experience — create minimal wrapper
            # Check if this is a failure entry
            attempts = []
            if entry.memory_type == MemoryType.FAILURE.value and "approach" in metadata:
                attempts.append(
                    Attempt(
                        approach=metadata.get("approach", ""),
                        outcome=AttemptOutcome.FAILURE.value,
                        reason=metadata.get("reason", "")
                    )
                )
            
            env_data = metadata.get("environment")
            env = EnvironmentContext.from_dict(env_data) if env_data else EnvironmentContext()
            
            repo_data = metadata.get("repository")
            repo = RepositoryContext.from_dict(repo_data) if repo_data else RepositoryContext()
                
            return cls(
                experience_id=str(entry.entry_id),
                task=entry.source_task or entry.category or "",
                problem=entry.text,
                attempts=attempts,
                environment=env,
                repository=repo,
                source_agent=entry.source_agent,
                confidence=entry.confidence,
                content_hash=metadata.get("content_hash", ""),
            )
        return cls._from_payload_dict(
            payload,
            experience_id=str(entry.entry_id),
            content_hash=metadata.get("content_hash", ""),
        )

    # ------------------------------------------------------------------
    # Internal payload serialization
    # ------------------------------------------------------------------

    def _to_payload_dict(self) -> Dict[str, Any]:
        return {
            "experience_id": self.experience_id,
            "task": self.task,
            "problem": self.problem,
            "attempts": [a.to_dict() for a in self.attempts],
            "solution": self.solution,
            "why_it_worked": self.why_it_worked,
            "verification_status": self.verification_status,
            "verification_method": self.verification_method,
            "verification_version": self.verification_version,
            "verification_evidence": [e.to_dict() for e in self.verification_evidence],
            "source_agent": self.source_agent,
            "source_session": self.source_session,
            "created_at": self.created_at,
            "repository": self.repository.to_dict(),
            "environment": self.environment.to_dict(),
            "confidence": self.confidence,
            "content_hash": self.content_hash,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def _from_payload_dict(
        cls,
        d: Dict[str, Any],
        experience_id: str = "",
        content_hash: str = "",
    ) -> "StructuredExperience":
        return cls(
            experience_id=experience_id or d.get("experience_id", ""),
            task=d.get("task", ""),
            problem=d.get("problem", ""),
            attempts=[Attempt.from_dict(a) for a in d.get("attempts") or []],
            solution=d.get("solution", ""),
            why_it_worked=d.get("why_it_worked", ""),
            verification_status=d.get("verification_status", VerificationStatus.CANDIDATE.value),
            verification_method=d.get("verification_method", ""),
            verification_version=d.get("verification_version", "1"),
            verification_evidence=[
                EvidenceRecord.from_dict(e) for e in d.get("verification_evidence") or []
            ],
            source_agent=d.get("source_agent", ""),
            source_session=d.get("source_session", ""),
            created_at=d.get("created_at", ""),
            repository=RepositoryContext.from_dict(d.get("repository") or {}),
            environment=EnvironmentContext.from_dict(d.get("environment") or {}),
            confidence=float(d.get("confidence") or 0.5),
            content_hash=content_hash or d.get("content_hash", ""),
            supersedes=d.get("supersedes"),
            superseded_by=d.get("superseded_by"),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

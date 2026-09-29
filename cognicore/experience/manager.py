"""Experience manager for StructuredExperience lifecycle.

Bridges :class:`StructuredExperience` (the validated, transferable agent
experience object) with the storage backend (typically
:class:`cognicore.memory.SQLiteMemoryBackend`).

The ExperienceManager is the orchestrator that the chatgpt integration
(and any other integration that wants to record/retrieve structured
experiences) talks to. It is intentionally a thin layer: it serializes
StructuredExperience objects into MemoryEntry.metadata["experience"]
for storage, and deserializes them back on retrieve.

Lifecycle (mirrors StructuredExperience docstring)::

    CANDIDATE -> OBSERVED -> VERIFIED -> PROMOTED -> TRANSFERABLE

CANDIDATE is what ``record()`` produces. ``verify()`` promotes to
VERIFIED if the supplied evidence is sufficient; otherwise the experience
stays CANDIDATE and the failure reason is returned to the caller.

This module was missing from the original implementation in commit
364768a (Sep 4 2026) - the chatgpt integration imported
``ExperienceManager`` from ``cognicore.experience``, but the class was
never defined and the ``__init__.py`` was never created. CI collection
errors on tests/test_chatgpt_*.py have been failing since then.

Refs: cognicore-dev/cognicore-env - CI failures on test (3.11) since
commit 364768a.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cognicore.experience.schema import (
    Attempt,
    AttemptOutcome,
    EnvironmentContext,
    EvidenceRecord,
    StructuredExperience,
    VerificationStatus,
)
from cognicore.memory.base import MemoryEntry, MemoryState

logger = logging.getLogger("cognicore.experience.manager")


# ------------------------------------------------------------------
# Result types
# ------------------------------------------------------------------


@dataclass
class RetrieveResult:
    """Result of an ExperienceManager.retrieve() call.

    Mirrors the shape that the chatgpt integration expects
    (results.total_candidates / .filtered_out / .experiences / .failures).
    """

    total_candidates: int = 0
    filtered_out: int = 0
    experiences: List[StructuredExperience] = field(default_factory=list)
    failures: List[StructuredExperience] = field(default_factory=list)


@dataclass
class VerifyResult:
    """Result of an ExperienceManager.verify() call.

    Mirrors the shape that the chatgpt integration expects
    (vresult.passed / .reason / .blockers / ._promoted_id).
    """

    passed: bool = False
    reason: str = ""
    blockers: List[str] = field(default_factory=list)
    _promoted_id: str = ""


# ------------------------------------------------------------------
# Manager
# ------------------------------------------------------------------


class ExperienceManager:
    """Orchestrates StructuredExperience persistence and retrieval.

    The manager is stateless except for the backend it wraps. Each call
    opens a fresh transaction on the backend; there is no in-memory cache.
    This is deliberate: the chatgpt integration creates a new manager
    per request, so caching would be wasted memory and a stale-cache
    bug waiting to happen.

    Storage strategy
    ----------------
    Each StructuredExperience is serialized to a MemoryEntry:

    - ``text``        = ``experience.task`` (searchable)
    - ``category``    = ``"experience"``
    - ``state``       = mirrors ``experience.verification_status``
    - ``memory_type`` = ``"experience"``
    - ``metadata["experience"]`` = full StructuredExperience.to_dict()

    The experience_id is stored as entry_id so retrieval by id maps
    cleanly to backend.get_by_id().
    """

    def __init__(self, backend: Any) -> None:
        """Wrap a storage backend.

        Parameters
        ----------
        backend
            Anything with a compatible subset of the
            :class:`SQLiteMemoryBackend` API: ``store(entry)``,
            ``search(query, top_k)``, ``get_by_id(id)``, ``get_all()``,
            ``get_by_state(state)``, ``update(entry_id, **fields)``,
            ``db_path``.
        """
        self.backend = backend

    # ------------------------------------------------------------------
    # Record
    # ------------------------------------------------------------------

    def record(self, experience: StructuredExperience) -> str:
        """Persist *experience* as a CANDIDATE in the backend.

        Returns the experience_id. This id is stored inside
        ``metadata["experience"].experience_id`` and is what callers
        should pass back to :meth:`verify` and :meth:`retrieve`.

        Note: the backend (SQLiteMemoryBackend) generates its own
        integer primary key for ``MemoryEntry.entry_id`` - we do NOT
        try to override that. The link between experience_id and the
        backend\'s storage id is held inside the metadata payload, so
        lookups by experience_id go through ``get_all`` + filter
        rather than ``get_by_id`` (which expects the backend\'s own id).
        """
        entry = self._experience_to_entry(experience)
        stored_id = self.backend.store(entry)
        logger.info(
            "recorded experience %s (backend_id=%s, status=%s, task=%r)",
            experience.experience_id,
            stored_id,
            experience.verification_status,
            experience.task[:80],
        )
        return experience.experience_id

    # ------------------------------------------------------------------
    # Retrieve
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        current_env: Optional[EnvironmentContext] = None,
        include_failures: bool = False,
        require_verified: bool = False,
        top_k: int = 5,
    ) -> RetrieveResult:
        """Retrieve experiences matching *query*.

        Parameters
        ----------
        query
            Free-text search over the experience.task and stored text.
            Empty string means "all experiences" (used by the chatgpt
            check endpoint).
        current_env
            If supplied, experiences whose environment is incompatible
            are filtered out (counted in ``filtered_out``).
        include_failures
            If True, experiences whose latest Attempt had outcome
            FAILURE are returned in ``failures`` instead of being
            discarded.
        require_verified
            If True, only VERIFIED or higher-status experiences are
            returned.
        top_k
            Maximum number of experiences to return.
        """
        # Pull candidates from the backend
        if query:
            entries = self.backend.search(query, top_k=top_k * 2)
        else:
            entries = self.backend.get_all(limit=top_k * 2)

        # Convert + filter
        experiences: List[StructuredExperience] = []
        failures: List[StructuredExperience] = []
        filtered_out = 0

        for raw_entry in entries:
            # ``backend.search()`` returns SearchResult objects (which
            # wrap a MemoryEntry at .entry). ``backend.get_all()`` returns
            # MemoryEntry directly. Normalize to MemoryEntry here so the
            # rest of the loop doesn't need to care which API path was taken.
            entry = getattr(raw_entry, "entry", raw_entry)
            exp = self._entry_to_experience(entry)
            if exp is None:
                continue

            # Filter by verification status
            if require_verified and not _is_verified_or_higher(exp):
                filtered_out += 1
                continue

            # Filter by environment compatibility
            if current_env is not None and not _env_compatible(exp.environment, current_env):
                filtered_out += 1
                continue

            # Split failures
            if _is_failure(exp) and include_failures:
                failures.append(exp)
            elif _is_failure(exp):
                # Discarded - caller asked not to include failures
                filtered_out += 1
            else:
                experiences.append(exp)

            if len(experiences) >= top_k and not include_failures:
                break

        total_candidates = len(entries)
        return RetrieveResult(
            total_candidates=total_candidates,
            filtered_out=filtered_out,
            experiences=experiences[:top_k],
            failures=failures[:top_k],
        )

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def verify(
        self,
        experience_id: str,
        evidence: List[EvidenceRecord],
    ) -> VerifyResult:
        """Promote a CANDIDATE experience to VERIFIED using *evidence*.

        Fail-closed: if the experience is not found, the result is
        ``passed=False`` with reason "experience_not_found". If the
        evidence list is empty, the result is ``passed=False`` with
        reason "no_evidence" and a single blocker
        "at_least_one_evidence_record_required".

        On success, the experience's verification_status is updated to
        VERIFIED and a new promoted_id is returned via
        ``VerifyResult._promoted_id`` (currently equal to the original
        experience_id; future TRANSFERABLE promotions may differ).
        """
        # Look up the experience by its experience_id (which lives inside
        # metadata["experience"].experience_id, NOT in the backend\'s
        # integer primary key). We scan get_all() and filter.
        all_entries = self.backend.get_all(limit=10000)
        entry = None
        for raw_entry in all_entries:
            candidate = getattr(raw_entry, "entry", raw_entry)
            meta = candidate.metadata or {}
            exp_data = meta.get("experience")
            if isinstance(exp_data, dict) and exp_data.get("experience_id") == experience_id:
                entry = candidate
                break
        if entry is None:
            return VerifyResult(
                passed=False,
                reason="experience_not_found",
                blockers=[f"no experience with id={experience_id}"],
            )

        exp = self._entry_to_experience(entry)
        if exp is None:
            return VerifyResult(
                passed=False,
                reason="deserialize_failed",
                blockers=["experience payload corrupt or missing"],
            )

        # Gate 1: at least one evidence record
        if not evidence:
            return VerifyResult(
                passed=False,
                reason="no_evidence",
                blockers=["at_least_one_evidence_record_required"],
            )

        # Gate 2: all evidence exit_code == 0 (success)
        failing = [e for e in evidence if e.exit_code != 0]
        if failing:
            return VerifyResult(
                passed=False,
                reason="evidence_failed",
                blockers=[
                    f"evidence_command={e.command} exit_code={e.exit_code}"
                    for e in failing
                ],
            )

        # Promote
        exp.verification_status = VerificationStatus.VERIFIED.value
        exp.verification_evidence = list(evidence)
        exp.verification_method = "manual_review_with_evidence"
        exp.verification_version = "1"

        # Persist updated entry
        updated_entry = self._experience_to_entry(exp)
        self.backend.store(updated_entry)

        return VerifyResult(
            passed=True,
            reason="verified",
            blockers=[],
            _promoted_id=exp.experience_id,
        )

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def _experience_to_entry(self, exp: StructuredExperience) -> MemoryEntry:
        """Serialize a StructuredExperience into a MemoryEntry for storage."""
        metadata: Dict[str, Any] = {"experience": exp.to_dict()}
        # Mirror key fields onto the MemoryEntry itself for searchability
        return MemoryEntry(
            text=exp.task or "",
            category="experience",
            action=exp.solution[:200] if exp.solution else "",
            metadata=metadata,
            memory_type="experience",
            state=_verification_status_to_memory_state(exp.verification_status),
            entry_id=exp.experience_id,
            confidence=exp.confidence,
            source_agent=exp.source_agent or "",
            source_task=exp.task,
            creation_reason="structured_experience",
            importance=0.7,  # experiences are weighted higher than generic memory
        )

    def _entry_to_experience(
        self, entry: MemoryEntry
    ) -> Optional[StructuredExperience]:
        """Deserialize a MemoryEntry back into a StructuredExperience.

        Returns None if the entry does not carry an experience payload
        (e.g. it's a non-experience memory entry that slipped into the
        search results).
        """
        meta = entry.metadata or {}
        exp_data = meta.get("experience")
        if not isinstance(exp_data, dict):
            return None
        try:
            return StructuredExperience.from_dict(exp_data)
        except Exception as exc:
            logger.warning(
                "failed to deserialize experience %s: %s",
                entry.entry_id, exc,
            )
            return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _is_verified_or_higher(exp: StructuredExperience) -> bool:
    """True if the experience is VERIFIED, PROMOTED, or TRANSFERABLE."""
    return exp.verification_status in (
        VerificationStatus.VERIFIED.value,
        VerificationStatus.PROMOTED.value,
        VerificationStatus.TRANSFERABLE.value,
    )


def _is_failure(exp: StructuredExperience) -> bool:
    """True if the experience's most recent attempt was a FAILURE."""
    if not exp.attempts:
        return False
    return exp.attempts[-1].outcome == AttemptOutcome.FAILURE.value


def _env_compatible(
    stored: EnvironmentContext,
    current: EnvironmentContext,
) -> bool:
    """Loose env compatibility check.

    Returns True if the environments are plausibly compatible. We do not
    enforce strict equality - that would break legitimate cross-platform
    reuse. The rule is: if both have a framework set and they differ,
    they're incompatible. Everything else (Python version, OS, deps)
    is treated as advisory.
    """
    if stored.framework and current.framework:
        return stored.framework.lower() == current.framework.lower()
    return True


def _verification_status_to_memory_state(status: str) -> str:
    """Map StructuredExperience.verification_status to MemoryState value.

    The MemoryEntry.state field is a free-form string but the canonical
    values are defined in MemoryState. We map by convention so that
    backend queries by state still work for callers that don't know
    about the experience schema.
    """
    mapping = {
        VerificationStatus.CANDIDATE.value: MemoryState.CANDIDATE.value,
        VerificationStatus.OBSERVED.value: MemoryState.OBSERVED.value,
        VerificationStatus.VERIFIED.value: MemoryState.VERIFIED.value,
        VerificationStatus.PROMOTED.value: MemoryState.PROMOTED.value,
        VerificationStatus.TRANSFERABLE.value: MemoryState.TRANSFERABLE.value,
    }
    return mapping.get(status, MemoryState.CANDIDATE.value)

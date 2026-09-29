"""CogniCore experience subsystem.

Public surface::

    from cognicore.experience import (
        ExperienceManager,        # orchestrator: record / retrieve / verify
        ExperienceExtractor,       # parse raw session data -> StructuredExperience
        StructuredExperience,      # the validated, transferable experience object
        Attempt,                   # one attempted solution
        AttemptOutcome,            # SUCCESS / FAILURE / UNKNOWN enum
        EvidenceRecord,            # signed proof of verification
        EnvironmentContext,        # python version, OS, framework, deps
        RepositoryContext,         # repo_id, commit, branch, affected files
        VerificationStatus,        # CANDIDATE -> OBSERVED -> VERIFIED -> PROMOTED -> TRANSFERABLE
    )

This module is the stable entry point. Downstream code (chatgpt
integration, exporters, importers) imports *only* from here, never from
the submodules directly. That keeps the package boundary explicit and
lets us refactor internals without breaking callers.

The ``ExperienceManager`` class was missing from the original
implementation in commit 364768a (Sep 4 2026) - the chatgpt integration
imported it, but neither the class nor this ``__init__.py`` existed.
CI collection errors on tests/test_chatgpt_*.py have been failing
since then because of this gap. Both were added in the fix that
introduced this file.

Refs: cognicore-dev/cognicore-env - CI failures on test (3.11) since
commit 364768a.
"""

from cognicore.experience.extractor import ExperienceExtractor
from cognicore.experience.manager import (
    ExperienceManager,
    RetrieveResult,
    VerifyResult,
)
from cognicore.experience.schema import (
    Attempt,
    AttemptOutcome,
    EnvironmentContext,
    EvidenceRecord,
    RepositoryContext,
    StructuredExperience,
    VerificationStatus,
)

__all__ = [
    # Manager + result types
    "ExperienceManager",
    "RetrieveResult",
    "VerifyResult",
    # Extractor
    "ExperienceExtractor",
    # Schema
    "StructuredExperience",
    "Attempt",
    "AttemptOutcome",
    "EvidenceRecord",
    "EnvironmentContext",
    "RepositoryContext",
    "VerificationStatus",
]

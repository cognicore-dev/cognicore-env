"""Semantic mutation for the verifier-defeat vector (recall-seam defeat).

Applied only when the test run passes ``--seam-mutation`` (see
``tests/conftest.py``); never active in production, in normal test runs,
or in CI jobs that do not opt in.

What it does
------------
It defeats the reachability guarantee at the recall seam: every query
"finds" every stored entry, so ``reachable_ids`` always covers
``imported_ids``, ``dark`` is always empty, and the reachability verdict
in ``importer.py`` can never fire.  The check line itself is left
byte-identical -- only its input is lied to.

This is the runtime-patch equivalent of the source-level mutation::

    - dark = imported_ids - reachable_ids
    + dark = set()

reviewed externally (2026-09-27, tested at ``27c0b89``, "mutation B"),
and it is deliberately *semantic* rather than textual:

  - it survives reformatting of ``importer.py`` (black, renames, type
    hints cannot make it vacuous),
  - it never edits shared disk state -- the mutation lives only inside
    the subprocess that requested it, so it is safe under pytest-xdist,
  - it fails loudly if its target moves: the patch addresses
    ``QuarantinePartition.search_trusted`` by name, so a refactor that
    renames or removes the recall seam raises ``AttributeError`` and the
    vector reports "target moved" instead of passing vacuously.

A moved check and a deleted check are both findings.

The injector carries its own positive control
---------------------------------------------
Installing a fault is not the same as the fault being *present*.  If the
live recall seam moves to a new name and the old one survives as a
back-compat shim, the rebinding below succeeds, the target guards pass,
and nothing observable changes: a no-op injection, indistinguishable from
a healthy detector.  That is the same failure class as a tripwire that
watches one spelling of the check -- it has only moved from the mutation's
*site* to the mutation's *effect*.

So the mutation verifies its own effect before the run proceeds: a query
that should match nothing must, under the patch, return everything, on
both recall paths.  Absence of an error while installing a fault is not
evidence that the fault exists.
"""

from __future__ import annotations

import pytest

from cognicore.memory.base import SearchResult


def apply_bright_recall_mutation() -> None:
    """Make every recall query 'succeed': all stored entries are reachable.

    The lie is told above the backend layer (``SQLiteMemoryBackend.search``
    / ``get_by_category``), so tests that patch those to simulate index
    lag -- the firing test's own seam -- are unaffected by construction:
    this patch intercepts the recall call first and short-circuits it.
    """
    from cognicore.integrations.mem0 import quarantine as quarantine_module
    from cognicore.memory.tfidf_backend import TFIDFMemoryBackend

    # Target 1: the trusted recall path (primary -- the firing test's dark
    # claim is a verified/trusted entry).
    def _bright_search_trusted(self, query: str, top_k: int = 5):
        entries = self.trusted.get_all()
        return [
            SearchResult(entry=e, score=1.0, source="mutation:bright-recall")
            for e in entries
        ]

    # Target 2: the quarantine recall path (same lie, other partition).
    def _bright_quarantine_search(self, query: str, top_k: int = 5):
        return [
            SearchResult(entry=e, score=1.0, source="mutation:bright-recall")
            for e in self.entries
        ]

    # Fail loudly if the recall seam moves.
    assert hasattr(quarantine_module.QuarantinePartition, "search_trusted"), (
        "mutation target drifted: QuarantinePartition.search_trusted no "
        "longer exists. Update the seam-mutation vector to the new recall "
        "seam -- a moved check and a deleted check are both findings."
    )
    assert hasattr(TFIDFMemoryBackend, "search"), (
        "mutation target drifted: TFIDFMemoryBackend.search no longer "
        "exists. Update the seam-mutation vector to the new quarantine "
        "recall seam."
    )

    quarantine_module.QuarantinePartition.search_trusted = _bright_search_trusted
    TFIDFMemoryBackend.search = _bright_quarantine_search

    # Positive control: the patch must be observably present on the live
    # paths, not merely installed. See module docstring.
    _assert_fault_is_observable()


def _assert_fault_is_observable() -> None:
    """Prove the bright-recall lie is actually told on both recall paths.

    A query that should return a restricted subset -- here, a nonsense
    token that matches nothing -- must, under the mutation, return every
    stored entry.  If it still returns nothing, the mutation bound to a
    seam that the live path no longer uses (typically a rename that left a
    back-compat shim behind), and the run must report *that*, rather than
    letting an unmutated system be read as a healthy detector.
    """
    import os
    import tempfile

    from cognicore.integrations.mem0.quarantine import QuarantinePartition
    from cognicore.memory.base import MemoryEntry
    from cognicore.memory.tfidf_backend import TFIDFMemoryBackend

    probe_query = "zzzqx-nonsense-token-that-matches-nothing"
    probe_text = "Use cmake to configure the build"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Live path 1: trusted partition (importer.py calls
        # partition.search_trusted(...)).
        partition = QuarantinePartition(storage_dir=tmpdir)
        try:
            partition.store_trusted(MemoryEntry(text=probe_text))
            trusted_results = partition.search_trusted(probe_query, top_k=5)
        finally:
            partition.close()

        # Live path 2: quarantine partition (importer.py calls
        # partition.quarantine.search(...)).
        backend = TFIDFMemoryBackend(
            persistence_path=os.path.join(tmpdir, "probe_quarantine.json")
        )
        backend.store(MemoryEntry(text=probe_text))
        quarantine_results = backend.search(probe_query, top_k=5)

    if not (trusted_results and quarantine_results):
        raise pytest.UsageError(
            "FAULT NOT PRESENT: the bright-recall mutation was installed but "
            "did not take effect -- a query that matches nothing still "
            f"returns nothing (trusted={len(trusted_results)}, "
            f"quarantine={len(quarantine_results)}). The live recall path is "
            "not the seam this mutation patched; the usual cause is a rename "
            "that left QuarantinePartition.search_trusted or "
            "TFIDFMemoryBackend.search behind as a shim while real recall "
            "moved elsewhere. Absence of an error while installing a fault "
            "is not evidence that the fault exists -- update the mutation to "
            "the new recall seam."
        )

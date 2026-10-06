"""Import a sealed transfer bundle into CogniCore with fail-closed verification.

Implements the full design review consensus from run-llama/llama_index#23122:

1. **Signature verification is the outer gate.**  Ed25519 over deterministic
   canonical JSON bundle.  No valid signature -> import fails.
   There is NO configuration flag, env var, or fallback mode that
   lets an unsigned or wrongly-signed bundle become memory.

2. **Fail-closed on empty sections.**  An empty or null ``proofs`` section
   is treated identically to a tampered bundle: parse-time failure with
   verdict ``INTEGRITY_FAILED``.

3. **Consequence-class split.**  Records whose ``consequence_class`` is
   ``authority`` are refused outright.  ``information`` records that are
   unverified or env-incompatible enter structural quarantine partition
   (``quarantine.json``, independent TF-IDF index and query path).  Valid
   verified memories with compatible environment land directly in the
   trusted partition (the main SQLite store, table ``memory_entries``).

4. **Promotion only via verification event.**  Never time, never
   repetition, never volume.

5. **Reachability assertion.**  After import, index rebuilt locally and
   every imported ID checked for retrievability through the index/recall path.
   Dark memory = failed import.

6. **Revocation is a hop.**  A custody chain containing a revocation of a
   signer the bundle depends on fails import with ``REVOKED``, even if the
   signature itself is valid.

7. **Environment compatibility.**  Records whose env fingerprint is
   incompatible import as ``OBSERVED`` in structural quarantine with
   ``invalidation_reason`` populated.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set

from cognicore.memory.base import MemoryEntry, MemoryState
from cognicore.memory_manager import MemoryManager

from cognicore.integrations.mem0.bundle import (
    ConsequenceClass,
    CustodyAction,
    ImportReceipt,
    ImportVerdict,
)
from cognicore.integrations.mem0.crypto import verify_signature
from cognicore.integrations.mem0.quarantine import QuarantinePartition

logger = logging.getLogger("cognicore.integrations.mem0.importer")


# ------------------------------------------------------------------
# Exceptions
# ------------------------------------------------------------------


class IntegrityFailed(Exception):
    """Raised when bundle integrity verification fails."""

    def __init__(
        self,
        message: str,
        verdict: ImportVerdict = ImportVerdict.INTEGRITY_FAILED,
    ):
        self.verdict = verdict
        super().__init__(message)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


# Quantization rules — reverse of exporter._entry_to_record.
# See #135 item 3.
_BASIS_POINT_FIELDS = (
    ("confidence_basis_points", "confidence"),
    ("importance_basis_points", "importance"),
    ("relevance_basis_points", "relevance"),
)
_MILLI_FIELDS = (("utility_score_milli", "utility_score"),)
_MS_FIELDS = (
    ("timestamp_ms", "timestamp"),
    ("last_accessed_ms", "last_accessed"),
)


def _dequantize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse the int→float quantization done by the exporter.

    The signed wire schema carries only int values (see #135 item 3).
    On import, we map them back to the float representation that the
    in-memory ``MemoryEntry`` expects. If a record already carries the
    float field directly (e.g. a bundle produced by an older exporter
    that pre-dates this rule), we leave it untouched — the float is
    still in memory, even though it should never have been on the wire.

    Malformed int values log a warning and fall back to the dataclass
    default rather than crashing the entire import.
    """
    out = dict(record)

    for src, dst in _BASIS_POINT_FIELDS:
        if src in out and dst not in out:
            try:
                out[dst] = float(out[src]) / 10000.0
            except (TypeError, ValueError):
                logger.warning(
                    "Malformed %s=%r on record %r; falling back to default %s",
                    src, out[src], out.get("entry_id", "unknown"), dst,
                )
                out.pop(src, None)

    for src, dst in _MILLI_FIELDS:
        if src in out and dst not in out:
            try:
                out[dst] = float(out[src]) / 1000.0
            except (TypeError, ValueError):
                logger.warning(
                    "Malformed %s=%r on record %r; falling back to default %s",
                    src, out[src], out.get("entry_id", "unknown"), dst,
                )
                out.pop(src, None)

    for src, dst in _MS_FIELDS:
        if src in out and dst not in out:
            try:
                out[dst] = float(out[src]) / 1000.0
            except (TypeError, ValueError):
                logger.warning(
                    "Malformed %s=%r on record %r; falling back to default %s",
                    src, out[src], out.get("entry_id", "unknown"), dst,
                )
                out.pop(src, None)

    return out


def _get_local_env_fingerprint() -> Dict[str, str]:
    """Capture the current environment's fingerprint."""
    return {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "os": platform.system().lower(),
    }


def _is_env_compatible(
    record_env: Dict[str, Any],
    local_env: Dict[str, str],
) -> bool:
    """Check if a memory's environment fingerprint is compatible."""
    if not record_env:
        return True  # No env info => cannot reject

    rec_python = record_env.get("python_version", "")
    local_python = local_env.get("python_version", "")

    if rec_python and local_python:
        rec_parts = rec_python.split(".")[:2]
        local_parts = local_python.split(".")[:2]
        if rec_parts and local_parts and rec_parts[0] != local_parts[0]:
            return False  # Major version mismatch

    rec_os = record_env.get("os", "").lower()
    local_os = local_env.get("os", "").lower()

    if rec_os and local_os and rec_os != local_os:
        return False

    return True


def _check_custody_revocations(
    custody: List[Dict[str, Any]],
    required_signer_ids: Set[str],
) -> Optional[str]:
    """Check custody chain for revocations of required signers.

    Returns the revoked ``signer_id`` if found, ``None`` if clean.
    """
    revoked_signers: Set[str] = set()

    for hop in custody:
        if hop.get("action") == CustodyAction.REVOKE.value:
            revoked_signers.add(hop.get("signer_id", ""))

    revoked_required = required_signer_ids & revoked_signers
    if revoked_required:
        return next(iter(revoked_required))

    return None


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def import_bundle(
    path: Path,
    target: MemoryManager,
    *,
    signer_keys: Mapping[str, Any],  # str -> Ed25519PublicKey
) -> ImportReceipt:
    """Import a sealed transfer bundle with fail-closed verification.

    Trusted records land in the main CogniCore SQLite store (table ``memory_entries``).
    Unverified or env-incompatible records land in structural quarantine (``quarantine.json``).

    Parameters
    ----------
    path:
        Path to the bundle JSON file.
    target:
        MemoryManager to import into.
    signer_keys:
        Mapping of ``signer_id`` to ``Ed25519PublicKey``.  The bundle's
        custody chain must reference a signer_id present in this mapping.

    Returns
    -------
    ImportReceipt
        Verdict and statistics.

    Raises
    ------
    IntegrityFailed
        On signature failure, empty proofs, tampering, or reachability
        failure.
    """
    path = Path(path)

    # ---- Parse ----
    try:
        with open(path, "r", encoding="utf-8") as f:
            bundle = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise IntegrityFailed(f"Bundle parse failed: {exc}")

    if not isinstance(bundle, dict):
        raise IntegrityFailed("Bundle is not a JSON object")

    # ---- Rule 2: fail-closed on empty sections ----
    proofs = bundle.get("proofs")
    if not proofs:
        raise IntegrityFailed(
            "Empty or null proofs section -- treated as tampered bundle",
            ImportVerdict.INTEGRITY_FAILED,
        )

    memories = bundle.get("memories")
    if not isinstance(memories, list):
        raise IntegrityFailed("Missing or invalid memories section")

    custody = bundle.get("custody")
    if not isinstance(custody, list) or not custody:
        raise IntegrityFailed("Missing or empty custody chain")

    signature_b64 = bundle.get("signature")
    if not signature_b64:
        raise IntegrityFailed(
            "Missing signature -- unsigned bundles are never accepted"
        )

    # ---- Identify the signing key ----
    signing_hop = None
    for hop in custody:
        if hop.get("action") == CustodyAction.SIGN.value:
            signing_hop = hop
            break

    if not signing_hop:
        raise IntegrityFailed("No SIGN hop in custody chain")

    signer_id = signing_hop.get("signer_id", "")
    if signer_id not in signer_keys:
        raise IntegrityFailed(
            f"Unknown signer {signer_id!r} -- not in trusted key set"
        )

    # ---- Rule 6: custody chain revocation check ----
    # A revocation of a signer the bundle depends on fails import with
    # REVOKED, even if the signature itself is valid and unexpired.
    revoked = _check_custody_revocations(custody, {signer_id})
    if revoked:
        return ImportReceipt(
            verdict=ImportVerdict.REVOKED,
            message=(
                f"Signer {revoked!r} has been revoked in the custody chain. "
                f"Import refused even though signature may be "
                f"cryptographically valid."
            ),
        )

    # ---- Rule 1: signature verification (outer gate) ----
    payload_for_verify = {k: v for k, v in bundle.items() if k != "signature"}

    public_key = signer_keys[signer_id]
    if not verify_signature(payload_for_verify, signature_b64, public_key):
        raise IntegrityFailed(
            "Ed25519 signature verification failed -- bundle rejected",
            ImportVerdict.INTEGRITY_FAILED,
        )

    # ---- Environment fingerprint ----
    local_env = _get_local_env_fingerprint()

    # ---- Process each memory record ----
    import_agent_id = (
        f"mem0_import_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    )
    import_dir = os.path.join(target.storage_dir, import_agent_id)
    os.makedirs(import_dir, exist_ok=True)

    db_path = getattr(target, "db_path", os.path.join(target.storage_dir, "cognicore_memory.db"))
    partition = QuarantinePartition(import_dir, db_path=db_path)

    trusted_entries: List[MemoryEntry] = []
    quarantined_entries: List[MemoryEntry] = []
    authority_refused = 0
    env_incompatible_count = 0

    is_mem0_origin = (
        bundle.get("origin") == "mem0"
        or bundle.get("exporter", "").lower().startswith("mem0")
    )

    for record in memories:
        if not isinstance(record, dict):
            continue

        # ---- Rule 3: consequence-class split ----
        cc = record.get(
            "consequence_class", ConsequenceClass.INFORMATION.value
        )
        if cc == ConsequenceClass.AUTHORITY.value:
            authority_refused += 1
            logger.warning(
                "Authority-class record %r refused.  To import authority "
                "data, use a direct credential provisioning path with "
                "proper access control.",
                record.get("entry_id", "unknown"),
            )
            continue

        # Build MemoryEntry from record
        # Reverse the float→int quantization done by exporter._entry_to_record
        # so the in-memory representation stays float (the runtime contract).
        # See #135 item 3 — floats are forbidden only on the *signed* wire
        # schema, not in runtime memory.
        record = _dequantize_record(record)
        entry = MemoryEntry.from_dict(record)

        if not entry.metadata:
            entry.metadata = {}
        entry.metadata["_import_source"] = "mem0_bridge"
        entry.metadata["_import_timestamp"] = datetime.now(
            timezone.utc
        ).isoformat()
        entry.metadata["_original_state"] = record.get("state", "")

        # Strip internal fields that should not survive transfer
        for key in ("_tfidf_vector", "_inserted_at_step", "consequence_class", "_quarantine"):
            entry.metadata.pop(key, None)

        # ---- Rule 7: environment compatibility ----
        record_env: Dict[str, Any] = {}
        exp_meta = entry.metadata.get("experience", {})
        if isinstance(exp_meta, dict):
            record_env = exp_meta.get("environment", {})

        env_compatible = _is_env_compatible(record_env, local_env)

        if not env_compatible:
            entry.invalidated_reason = (
                f"Environment incompatible: record env={record_env}, "
                f"local env={local_env}"
            )
            env_incompatible_count += 1
            partition.store_quarantined(entry)
            quarantined_entries.append(entry)
        else:
            # Check if record is verified or must enter quarantine
            rec_state = (record.get("state") or "").lower()
            is_verified = rec_state in (
                MemoryState.VERIFIED.value,
                MemoryState.PROMOTED.value,
                MemoryState.TRANSFERABLE.value,
            )
            was_observed = record.get("_observed_at_export", False) or not is_verified

            if is_mem0_origin or was_observed:
                # Mem0 export-back or unverified candidate enters structural quarantine
                partition.store_quarantined(entry)
                quarantined_entries.append(entry)
            else:
                # Verified memory with compatible env lands in main SQLite store!
                entry.state = record.get("state", MemoryState.VERIFIED.value)
                partition.store_trusted(entry)
                trusted_entries.append(entry)

    imported_entries = trusted_entries + quarantined_entries

    # ---- Flush quarantine region to disk ----
    # Since #149, TFIDFMemoryBackend.store() persists lazily: it marks
    # _dirty instead of writing synchronously. import_bundle owns this
    # partition's lifecycle, so the quarantine backend must be flushed
    # here -- before the reachability assertion can raise and before the
    # process that ran the import exits. Without this flush, quarantined
    # records live only in memory and a fresh QuarantinePartition opened
    # on the same session dir finds an empty quarantine.json (silent data
    # loss for every downstream consumer of the bridge).
    partition.quarantine.save()

    # ---- All-authority bundle ----
    if authority_refused > 0 and not imported_entries:
        return ImportReceipt(
            verdict=ImportVerdict.AUTHORITY_REFUSED,
            authority_refused=authority_refused,
            message=(
                f"All {authority_refused} record(s) in this bundle are "
                f"authority-class and were refused.  To import credentials "
                f"or permissions, use a direct provisioning path with "
                f"proper access control -- not the memory transfer bridge."
            ),
        )

    # ---- Rule 5: reachability assertion through index/recall path ----
    # Rebuild the index from imported memories, then verify every record
    # is retrievable through the index / search recall path.
    imported_ids = {e.entry_id for e in imported_entries}
    reachable_ids: Set[str] = set()

    for entry in trusted_entries:
        query = entry.text or entry.category or ""
        results = partition.search_trusted(query, top_k=max(10, len(trusted_entries)))
        for r in results:
            r_id = (r.entry.metadata or {}).get("bundle_entry_id") or r.entry.entry_id
            if r_id == entry.entry_id or r.entry.entry_id == entry.entry_id:
                reachable_ids.add(entry.entry_id)
                break
        else:
            cat_results = partition.trusted.get_by_category(
                entry.category, top_k=max(10, len(trusted_entries))
            )
            for r_entry in cat_results:
                r_id = (r_entry.metadata or {}).get("bundle_entry_id") or r_entry.entry_id
                if r_id == entry.entry_id or r_entry.entry_id == entry.entry_id:
                    reachable_ids.add(entry.entry_id)
                    break

    for entry in quarantined_entries:
        query = entry.text or entry.category or ""
        results = partition.quarantine.search(query, top_k=max(10, len(quarantined_entries)))
        if any(r.entry.entry_id == entry.entry_id for r in results):
            reachable_ids.add(entry.entry_id)
        else:
            cat_results = partition.quarantine.get_by_category(
                entry.category, top_k=max(10, len(quarantined_entries))
            )
            if any(e.entry_id == entry.entry_id for e in cat_results):
                reachable_ids.add(entry.entry_id)

    dark = imported_ids - reachable_ids
    if dark:
        raise IntegrityFailed(
            "reachability: %d/%d imported and not retrievable through index"
            % (len(dark), len(imported_ids)),
            ImportVerdict.INTEGRITY_FAILED,
        )

    # ---- Persist metadata ----
    meta = {
        "agent_id": import_agent_id,
        "source_bundle": str(path),
        "import_timestamp": datetime.now(timezone.utc).isoformat(),
        "signer_id": signer_id,
        "total_imported": len(imported_entries),
        "trusted": len(trusted_entries),
        "quarantined": len(quarantined_entries),
        "authority_refused": authority_refused,
        "env_incompatible": env_incompatible_count,
        "sqlite_db_path": db_path,
    }
    meta_path = os.path.join(import_dir, "metadata.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    return ImportReceipt(
        verdict=ImportVerdict.OK,
        total_imported=len(imported_entries),
        trusted=len(trusted_entries),
        quarantined=len(quarantined_entries),
        authority_refused=authority_refused,
        env_incompatible=env_incompatible_count,
        message=(
            f"Import successful: {len(trusted_entries)} trusted (in SQLite main store), "
            f"{len(quarantined_entries)} quarantined."
        ),
    )

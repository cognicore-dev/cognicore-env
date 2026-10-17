"""Chain test for the verifier-defeat chain.

**Absence of a link is a red test, not a missing import.**

The reachability guarantee is enforced by a chain of four links, each one
found missing the same way -- someone asked "what does this green actually
prove?" (thread: run-llama/llama_index#23122):

1. **importer control** -- ``importer.py``'s fail-closed verdict:
   ``dark = imported_ids - reachable_ids; if dark: IntegrityFailed``.
   Subject: imported entries must be retrievable through the live index.
2. **detector control** -- ``TestReachabilityFires::test_dark_claim_fires_import``
   imports a bundle whose dark claim is unreachable at the recall seam and
   requires the verdict to fire. Subject: the importer control.
3. **injector control** -- ``tests/seam_mutation.py``'s bright-recall mutation,
   whose positive control (``_assert_fault_is_observable``) proves the fault
   it installs is observably present. Subject: the fault itself.
4. **defeat vector** -- ``TestVerifierDefeatVector`` runs the detector in a
   subprocess under the mutation and requires ``DID NOT RAISE``. Subject: the
   detector going red when the importer control is defeated.

Links 1-3 carry their own positive controls; the vector (link 4) is guarded
only by this test. What was missing until now is anything that protects the
*chain's topology* from the repo moving underneath it: a renamed detector
module, a path change in the subprocess invocation, a moved recall seam --
each is a way for a link to vanish while everything downstream keeps
collecting green. This test enumerates the four links by name and asserts,
for each, that (a) it exists at its registered path, (b) it runs, and
(c) it fails when its own subject is disabled. Clause (c) is what makes this
a test rather than an import check.

Every probe runs in a subprocess (pytest or ``python -c``) with its own
temporary directories, exactly like the defeat vector: nothing here writes
shared disk state, so the test is safe under pytest-xdist by construction.

The registration below is deliberately dumb -- a dict of names and paths.
If a link drifts, update the registry *and* the probe that exercises it;
a moved link and a deleted link are both findings.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- the chain, registered ---------------------------------------------
CHAIN = {
    "importer control": {
        "registered": "cognicore.integrations.mem0.importer.import_bundle",
        "guards": (
            "fail-closed reachability verdict "
            "(dark = imported_ids - reachable_ids; if dark: IntegrityFailed)"
        ),
    },
    "detector control": {
        "registered": (
            "tests/test_mem0_bridge.py::TestReachabilityFires"
            "::test_dark_claim_fails_import"
        ),
        "guards": "the importer control fires on a dark claim",
    },
    "injector control": {
        "registered": (
            "tests/seam_mutation.py::apply_bright_recall_mutation "
            "+ _assert_fault_is_observable"
        ),
        "guards": "the bright-recall fault is observably present when installed",
    },
    "defeat vector": {
        "registered": (
            "tests/test_mem0_bridge.py::TestVerifierDefeatVector"
            "::test_recall_seam_defeat_makes_firing_test_red"
        ),
        "guards": "the detector goes red when the importer control is defeated",
    },
}

DETECTOR_NODE = CHAIN["detector control"]["registered"]
VECTOR_NODE = CHAIN["defeat vector"]["registered"]
SUBPROCESS_TIMEOUT = 300  # seconds; matches the defeat vector's own budget


class TestChainLinks:
    """One test, four links, all failures named.

    The probes below each simulate exactly one subject-disabled case and
    require the corresponding link to answer for it. Findings accumulate
    rather than short-circuit, so one red run reports every lost link.
    """

    def _run_pytest(self, node, extra_args=()):
        cmd = [
            sys.executable, "-m", "pytest", node, "-q", "--no-header",
            *extra_args,
        ]
        return subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )

    def _tail(self, proc):
        return (proc.stdout + proc.stderr)[-1500:]

    def _probe_importer_and_detector_green(self, findings):
        """Links 1-2, clauses (a)+(b)+(c), one run.

        The healthy detector run exercises the importer control end to end:
        a dark claim (subject disabled: unreachable through the index) must
        make ``import_bundle`` fail closed. Green here means both links
        exist at their registered paths, run, and fire for the right
        reason.
        """
        proc = self._run_pytest(DETECTOR_NODE)
        out = proc.stdout + proc.stderr
        if "no tests ran" in out:
            findings.append(
                "CHAIN LINK LOST: detector control -- the firing test was not "
                f"collected at its registered path ({DETECTOR_NODE}). A moved "
                "detector and a deleted detector are both findings; update the "
                "registry and the defeat vector's DETECTOR to the new location. "
                "The importer control cannot be exercised without it.\n"
                + self._tail(proc)
            )
            return
        if proc.returncode != 0:
            findings.append(
                "CHAIN LINK LOST: importer control -- the detector run went red "
                "on a HEALTHY system. Either the fail-closed verdict did not "
                "fire on the dark claim (importer control degraded) or the "
                "detector itself broke; both are findings.\n"
                + self._tail(proc)
            )

    def _probe_detector_red_under_mutation(self, findings):
        """Links 2-3, clause (c) for the detector, (b) for the injector.

        The same detector run under ``--seam-mutation``: the bright-recall
        patch defeats the importer control (subject disabled), so the
        detector must FAIL with ``DID NOT RAISE``. The injector's positive
        control runs inside the same subprocess -- a FAULT NOT PRESENT stop
        is a finding about link 3, not link 2.
        """
        proc = self._run_pytest(DETECTOR_NODE, extra_args=["--seam-mutation"])
        out = proc.stdout + proc.stderr
        if "no tests ran" in out:
            findings.append(
                "CHAIN LINK LOST: detector control -- not collected at its "
                f"registered path ({DETECTOR_NODE}) under the mutation run.\n"
                + self._tail(proc)
            )
            return
        if "FAULT NOT PRESENT" in out:
            findings.append(
                "CHAIN LINK LOST: injector control -- the bright-recall "
                "mutation was installed but its own positive control "
                "reported the fault absent. The live recall path is not the "
                "seam the mutation patched; update the mutation to the new "
                "recall seam.\n" + self._tail(proc)
            )
            return
        if "DID NOT RAISE" not in out or proc.returncode == 0:
            findings.append(
                "CHAIN LINK LOST: detector control -- reachability was "
                "defeated at the recall seam and the firing test did not go "
                "red with DID NOT RAISE. A check nobody has watched fail is "
                "a promise, not a guarantee.\n" + self._tail(proc)
            )

    def _probe_injector_control_fires_on_absent_fault(self, findings):
        """Link 3, clause (c): red when the fault is absent.

        Runs the positive control WITHOUT installing the mutation -- the
        same observable state as a no-op injection (a rename that left a
        back-compat shim behind): the probe query matches nothing, so the
        control must stop the run with FAULT NOT PRESENT. A control that
        only ever sees the fault present is an import check.
        """
        script = (
            "from tests.seam_mutation import _assert_fault_is_observable\n"
            "_assert_fault_is_observable()\n"
            "print('CHAIN-INJECTOR-UNEXPECTEDLY-GREEN')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        out = proc.stdout + proc.stderr
        if "FAULT NOT PRESENT" not in out:
            findings.append(
                "CHAIN LINK LOST: injector control -- the positive control "
                "did not fire when the fault was absent (no mutation "
                "installed). It must raise FAULT NOT PRESENT on the no-op/"
                "shim state, or a dead injection reads as a healthy "
                "detector.\n" + self._tail(proc)
            )

    def _probe_defeat_vector_fires_on_lost_detector(self, findings):
        """Link 4, clauses (a)+(b)+(c).

        Simulates the structural drift this link exists to catch: the
        detector node is redirected to a path that no longer exists, and
        the vector must go red naming the loss (MUTATION TARGET ORPHANED)
        rather than pass vacuously. Executing the vector method here also
        proves it still runs -- a vector that cannot even run is a finding
        about the vector, not about what it watches.
        """
        script = (
            "import sys\n"
            "from tests.test_mem0_bridge import TestVerifierDefeatVector\n"
            "# structural drift: the detector has moved; the registered node\n"
            "# below does not exist.\n"
            "TestVerifierDefeatVector.DETECTOR = (\n"
            "    'tests/test_mem0_bridge.py::TestReachabilityFires'\n"
            "    '::test_dark_claim_fails_import_RENAMED'\n"
            ")\n"
            "vector = TestVerifierDefeatVector()\n"
            "try:\n"
            "    vector.test_recall_seam_defeat_makes_firing_test_red()\n"
            "except AssertionError as exc:\n"
            "    msg = str(exc)\n"
            "    if 'MUTATION TARGET ORPHANED' in msg:\n"
            "        print('CHAIN-VECTOR-RED-OK')\n"
            "        sys.exit(0)\n"
            "    print('CHAIN-VECTOR-WRONG-REASON: ' + msg[:800])\n"
            "    sys.exit(3)\n"
            "print('CHAIN-VECTOR-UNEXPECTEDLY-GREEN')\n"
            "sys.exit(3)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        out = proc.stdout + proc.stderr
        if "CHAIN-VECTOR-RED-OK" not in out:
            findings.append(
                "CHAIN LINK LOST: defeat vector -- with the detector node "
                f"deliberately orphaned, the vector ({VECTOR_NODE}) did not "
                "go red with MUTATION TARGET ORPHANED. Either it can no "
                "longer run at all, or it passes while testing something "
                "other than what the thread agreed it tests.\n"
                + self._tail(proc)
            )

    def _probe_imports_exist(self, findings):
        """Clause (a) for the two links whose registered paths are modules."""
        try:
            importer = importlib.import_module(
                CHAIN["importer control"]["registered"].rsplit(".", 1)[0]
            )
            for symbol in ("import_bundle", "IntegrityFailed", "ImportVerdict"):
                if not hasattr(importer, symbol):
                    findings.append(
                        "CHAIN LINK LOST: importer control -- "
                        f"'{symbol}' is gone from "
                        f"{CHAIN['importer control']['registered']}. Update "
                        "the registry; a moved verdict is a finding."
                    )
        except Exception as exc:  # ImportError and friends
            findings.append(
                "CHAIN LINK LOST: importer control -- "
                f"{CHAIN['importer control']['registered']} no longer "
                f"imports ({exc}). Update the registry."
            )
        try:
            seam = importlib.import_module("tests.seam_mutation")
            for symbol in (
                "apply_bright_recall_mutation", "_assert_fault_is_observable"
            ):
                if not hasattr(seam, symbol):
                    findings.append(
                        "CHAIN LINK LOST: injector control -- "
                        f"'{symbol}' is gone from tests/seam_mutation.py. "
                        "Update the registry and the probes."
                    )
        except Exception as exc:
            findings.append(
                "CHAIN LINK LOST: injector control -- tests/seam_mutation.py "
                f"no longer imports ({exc}). Update the registry and the "
                "probes."
            )

    def test_chain_complete(self):
        findings: list[str] = []
        self._probe_imports_exist(findings)
        self._probe_importer_and_detector_green(findings)
        self._probe_detector_red_under_mutation(findings)
        self._probe_injector_control_fires_on_absent_fault(findings)
        self._probe_defeat_vector_fires_on_lost_detector(findings)
        assert not findings, (
            "VERIFIER CHAIN INCOMPLETE: %d of 4 links report findings.\n\n%s"
            % (len(findings), "\n\n".join(findings))
        )

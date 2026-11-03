"""The deploy gate decides what reaches production, so pin its behaviour.

The script deploys a commit only when the test matrix passed on that exact
commit. Two mistakes would be expensive: deploying a commit whose tests failed,
and refusing to deploy because an unrelated workflow (docs, dashboards,
scanners) is red. Both are covered here.

The Python gate is extracted from the shipped script rather than copied, so a
change to the script is a change to what these tests exercise.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "auto-deploy.sh"
REQUIRED_RE = r"^test \(3\.[0-9]+\)$"


def _gate_source():
    """The Python heredoc inside auto-deploy.sh that classifies check runs."""
    text = SCRIPT.read_text(encoding="utf-8")
    body = re.search(r"read -r -d '' CHECK_VERDICT_PY <<'PY'[^\n]*\n(.*?)\nPY\n", text, re.DOTALL)
    assert body, "could not find the check-run gate in auto-deploy.sh"
    return body.group(1)


def _verdict(check_runs):
    result = subprocess.run(
        [sys.executable, "-c", _gate_source()],
        input=json.dumps({"check_runs": check_runs}),
        capture_output=True, text=True, check=False,
        env={"REQUIRED_RE": REQUIRED_RE, "PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


TESTS_GREEN = [_run(f"test (3.{v})") for v in (10, 11, 12)]


def test_green_matrix_deploys():
    assert _verdict(TESTS_GREEN).startswith("passed")


def test_unrelated_failing_workflow_does_not_block():
    """GitHub Pages and the scanners must not be able to hold up a deploy."""
    runs = TESTS_GREEN + [
        _run("build", conclusion="failure"),
        _run("deploy", conclusion="skipped"),
        _run("📈 Project Health Dashboard", conclusion="failure"),
    ]
    assert _verdict(runs).startswith("passed")


def test_one_failing_test_job_blocks():
    runs = [_run("test (3.10)", conclusion="failure")] + TESTS_GREEN[1:]
    verdict = _verdict(runs)
    assert verdict.startswith("failed")
    assert "test (3.10)" in verdict


@pytest.mark.parametrize("status", ["in_progress", "queued"])
def test_unfinished_tests_wait_rather_than_deploy(status):
    runs = [_run("test (3.10)", status=status, conclusion=None)] + TESTS_GREEN[1:]
    assert _verdict(runs).startswith("pending")


def test_no_reported_tests_is_not_treated_as_success():
    """A commit CI has not reached yet must never look deployable."""
    assert _verdict([_run("build")]).startswith("pending")


def test_a_new_python_version_is_gated_automatically():
    """Adding 3.13 to the matrix must gate it without editing the script."""
    runs = TESTS_GREEN + [_run("test (3.13)", conclusion="failure")]
    assert _verdict(runs).startswith("failed")


def test_script_is_executable_and_valid_bash():
    assert SCRIPT.exists(), "deploy/auto-deploy.sh is missing"
    # A relative path: Windows backslashes would be mangled by bash.
    syntax = subprocess.run(["bash", "-n", "deploy/auto-deploy.sh"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr


def test_systemd_units_point_at_the_script():
    service = (ROOT / "deploy" / "systemd" / "cognicore-deploy.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "systemd" / "cognicore-deploy.timer").read_text(encoding="utf-8")
    assert "deploy/auto-deploy.sh" in service
    assert "Unit=cognicore-deploy.service" in timer
    assert "WantedBy=timers.target" in timer


def test_script_never_hardcodes_a_token():
    """The VM must hold no GitHub credential; the repository is public."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "Authorization:" not in text
    assert not re.search(r"gh[pousr]_[A-Za-z0-9]{20,}", text)

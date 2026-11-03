"""The deploy script decides what reaches production, so pin its behaviour.

deploy/auto-deploy.sh deploys a commit only when the CI workflow passed on that
exact commit, and holds changes that could reach the host for a person. These
tests run the pieces of the shipped script itself (the CI gate and the hold
patterns are extracted from it, not copied), so changing the script changes
what is tested.

The expensive mistakes are deploying untested code, being fooled by a result
that looks like CI but is not, auto-deploying a change that could escape the
container, and refusing to deploy because an unrelated workflow is red.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "auto-deploy.sh"
SERVICE = ROOT / "deploy" / "systemd" / "cognicore-deploy.service"
TIMER = ROOT / "deploy" / "systemd" / "cognicore-deploy.timer"

SLUG = "cognicore-dev/cognicore-env"
TARGET = "a" * 40
CI = ".github/workflows/ci.yml"


def _script():
    return SCRIPT.read_text(encoding="utf-8")


def _gate_source():
    """The Python heredoc inside auto-deploy.sh that judges the workflow runs."""
    body = re.search(r"read -r -d '' CI_VERDICT_PY <<'PY'[^\n]*\n(.*?)\nPY\n", _script(), re.DOTALL)
    assert body, "could not find the CI gate in auto-deploy.sh"
    return body.group(1)


def _verdict(workflow_runs):
    result = subprocess.run(
        [sys.executable, "-c", _gate_source()],
        input=json.dumps({"workflow_runs": workflow_runs}),
        capture_output=True, text=True, check=False,
        env={"WORKFLOW_PATH": CI, "BRANCH": "main", "SLUG": SLUG, "TARGET": TARGET, "PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _run(path=CI, *, status="completed", conclusion="success", event="push", branch="main",
         sha=TARGET, repo=SLUG, created="2026-11-03T10:00:00Z", attempt=1, name="CogniCore CI"):
    return {
        "id": 1, "name": name, "path": path, "event": event, "head_branch": branch,
        "head_sha": sha, "head_repository": {"full_name": repo}, "status": status,
        "conclusion": conclusion, "created_at": created, "run_attempt": attempt,
    }


# ── The CI gate ──────────────────────────────────────────────────────────────

def test_passing_ci_deploys():
    assert _verdict([_run()]).startswith("passed")


def test_unrelated_failing_workflows_do_not_block():
    """GitHub Pages and the scanners must not be able to hold up a deploy."""
    runs = [
        _run(),
        _run("dynamic/pages/pages-build-deployment", conclusion="failure", event="dynamic"),
        _run(".github/workflows/healthbot.yml", conclusion="failure"),
    ]
    assert _verdict(runs).startswith("passed")


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_anything_but_success_blocks(conclusion):
    assert _verdict([_run(conclusion=conclusion)]).startswith("failed")


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_unfinished_ci_waits_rather_than_deploys(status):
    assert _verdict([_run(status=status, conclusion=None)]).startswith("pending")


def test_no_ci_run_is_never_treated_as_success():
    assert _verdict([]).startswith("pending")


def test_a_lookalike_workflow_cannot_stand_in_for_ci():
    """A job or app can call itself "CogniCore CI"; only the real workflow file counts."""
    fake = _run(".github/workflows/totally-ci.yml", name="CogniCore CI")
    assert _verdict([fake]).startswith("pending")


@pytest.mark.parametrize("override", [
    {"repo": "attacker/cognicore-env"},   # a fork's run
    {"event": "pull_request"},            # a PR run, not the merged commit
    {"event": "workflow_dispatch"},       # started by hand with any inputs
    {"branch": "feature"},                # another branch
    {"sha": "b" * 40},                    # another commit
])
def test_ci_must_be_the_push_to_this_branch_of_this_repo(override):
    assert _verdict([_run(**override)]).startswith("pending")


def test_the_latest_run_decides():
    """An earlier failure followed by a passing run of the same commit deploys."""
    runs = [_run(conclusion="failure", created="2026-11-03T10:00:00Z"),
            _run(conclusion="success", created="2026-11-03T10:05:00Z")]
    assert _verdict(runs).startswith("passed")


def test_a_rerun_attempt_supersedes_the_first():
    runs = [_run(conclusion="failure", attempt=1), _run(conclusion="success", attempt=2)]
    assert _verdict(runs).startswith("passed")


# ── Holding changes that could reach the host ────────────────────────────────

def _held(path):
    script = _script()
    infra = re.search(r"^INFRA_RE='([^']*)'", script, re.MULTILINE).group(1)
    exempt = re.search(r"^INFRA_EXEMPT_RE='([^']*)'", script, re.MULTILINE).group(1)
    return bool(re.search(infra, path)) and not re.search(exempt, path)


@pytest.mark.parametrize("path", [
    "deploy/docker-compose.yml",   # mounts, privileges, capabilities
    "deploy/Caddyfile",
    "deploy/auto-deploy.sh",       # the deploy logic itself
    "deploy/systemd/cognicore-deploy.service",
    "deploy/setup-host.sh",
    "deploy/backup-to-blob.sh",
])
def test_host_level_changes_are_held(path):
    assert _held(path)


@pytest.mark.parametrize("path", [
    "cognicore/connectors/remote.py",
    "deploy/requirements.txt",     # the image's Python deps are application code
    "deploy/requirements.in",
    "Dockerfile",                  # builds run in a container, not on the host
    "README.md",
])
def test_application_changes_deploy_automatically(path):
    assert not _held(path)


# ── Install and configuration ────────────────────────────────────────────────

def test_script_is_valid_bash():
    # A relative path: Windows backslashes would be mangled by bash.
    syntax = subprocess.run(["bash", "-n", "deploy/auto-deploy.sh"], cwd=ROOT,
                            capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr


def test_root_runs_an_installed_copy_not_the_checkout():
    """If systemd ran the checkout's script, any commit could change what root executes."""
    exec_start = re.search(r"^ExecStart=(\S+)", SERVICE.read_text(encoding="utf-8"), re.MULTILINE).group(1)
    assert exec_start == "/usr/local/sbin/cognicore-deploy"
    assert "/opt/cognicore-env" not in exec_start


def test_timer_triggers_the_service():
    timer = TIMER.read_text(encoding="utf-8")
    assert "Unit=cognicore-deploy.service" in timer
    assert "WantedBy=timers.target" in timer


def test_git_cannot_run_code_from_the_checkouts_config():
    """Root reads a checkout owned by another user: hooks and fsmonitor stay off."""
    script = _script()
    assert "core.hooksPath=/dev/null" in script
    assert "core.fsmonitor=false" in script


def test_no_credential_is_kept_on_the_vm():
    script = _script()
    assert "Authorization:" not in script
    assert not re.search(r"gh[pousr]_[A-Za-z0-9]{20,}", script)


def test_deploy_and_rollback_use_exact_commits():
    """Deploying a branch name could pick up a commit pushed after CI was checked."""
    script = _script()
    assert 'reset --hard --quiet "$target"' in script
    assert 'reset --hard --quiet "$current"' in script


# ── Regressions from the 2026-11-03 outage ───────────────────────────────────
# The unit ran the script with UMask=0077, root's git rewrote changed source
# files as 0600, Docker copied that mode into the image, and the non-root app
# crash-looped on "Permission denied". The rollback rewrote files the same way,
# reported success without checking, and the timer retried every 3 minutes.

def _function_body(name):
    script = _script()
    match = re.search(rf"^{name}\(\) \{{\n(.*?)\n\}}\n", script, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in auto-deploy.sh"
    return match.group(1)


def test_unit_umask_cannot_hide_files_from_other_users():
    service = SERVICE.read_text(encoding="utf-8")
    umask = re.search(r"^UMask=([0-7]+)", service, re.MULTILINE)
    if umask:
        assert not int(umask.group(1), 8) & 0o004, f"UMask={umask.group(1)} strips read for others"


def test_git_runs_as_the_checkout_owner_not_root():
    script = _script()
    assert re.search(r'^GIT=\(runuser -u "\$repo_owner"', script, re.MULTILINE)
    assert 'repo_owner=$(stat -c %U "$REPO_DIR")' in script
    # Every git call goes through the wrapper.
    bare = [line for line in script.splitlines()
            if re.match(r"\s*git\s", line) and "-c core.hooksPath" not in line]
    assert not bare, f"git called without the owner wrapper: {bare}"


def test_unreadable_source_stops_the_deploy_before_anything_restarts():
    script = _script()
    guard = script.index("! -perm -o=r")
    first_restart = script.index('"${COMPOSE[@]}" up -d --build >&2; then')
    assert guard < first_restart


def test_rollback_must_prove_the_server_is_healthy():
    body = _function_body("rollback")
    assert "server_healthy" in body
    assert "exit 2" in body, "a rollback that does not come back must say the server is down"


def test_a_failed_commit_is_not_retried_every_tick():
    script = _script()
    assert '"$STATE_DIR/failed"' in _function_body("rollback")
    assert re.search(r'cat "\$STATE_DIR/failed".*== "\$target" && \$RETRY -eq 0', script)
    assert "--retry)" in script

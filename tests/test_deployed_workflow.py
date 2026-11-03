"""The "Deployed" workflow tells the team whether a merge is live.

Its step script runs here for real, against a throwaway git repository, with
`curl` answering as the server would and `gh` recording the commit status it is
asked to set. Teammates read that status instead of logging into the VM, so a
wrong answer (claiming a commit is live when it is not, or blaming the wrong
cause) is what these tests guard against.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "deployed.yml"

yaml = pytest.importorskip("yaml")
BASH = shutil.which("bash")
GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(not (BASH and GIT), reason="needs bash and git")


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step_script():
    steps = _workflow()["jobs"]["live"]["steps"]
    return next(s["run"] for s in steps if "run" in s)


def _git(repo, *args):
    out = subprocess.run([GIT, "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _commit(repo, path, text, message):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _stub(bin_dir, name, body):
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def world(tmp_path):
    """origin with main, a clone checked out like the workflow does, and stubs."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    shas = {
        "base": _commit(origin, "app.py", "1", "base"),
        "app_change": _commit(origin, "app.py", "2", "app change"),
        "deploy_change": _commit(origin, "deploy/docker-compose.yml", "x", "deploy change"),
        "later": _commit(origin, "app.py", "3", "later"),
    }
    work = tmp_path / "work"
    subprocess.run([GIT, "clone", "-q", str(origin), str(work)], check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    statuses = tmp_path / "statuses.log"
    # curl prints whatever the "server" currently reports.
    _stub(bin_dir, "curl", f'cat "{(tmp_path / "health.json").as_posix()}"\n')
    # gh records each status it is asked to set.
    _stub(bin_dir, "gh", f'printf "%s\\n" "$*" >> "{statuses.as_posix()}"\n')
    # The runner's python3 may not exist on this machine; point it at ours.
    _stub(bin_dir, "python3", f'exec "{Path(sys.executable).as_posix()}" "$@"\n')
    script = tmp_path / "step.sh"
    script.write_text(_step_script(), encoding="utf-8", newline="\n")

    def run(sha, live, ci="success"):
        (tmp_path / "health.json").write_text(json.dumps({"status": "ok", "commit": live}), encoding="utf-8")
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "SHA": sha, "CI_CONCLUSION": ci, "HEALTH_URL": "https://example.test/health",
            "WAIT_MINUTES": "0", "REPO": "o/r", "GH_TOKEN": "x",
        }
        result = subprocess.run([BASH, str(script)], cwd=work, env=env,
                                capture_output=True, text=True, check=False)
        log = statuses.read_text(encoding="utf-8") if statuses.exists() else ""
        statuses.unlink(missing_ok=True)
        return result.returncode, log, result.stdout + result.stderr

    return shas, run


def _final_state(log):
    return [line for line in log.splitlines() if "state=" in line][-1]


def test_the_merged_commit_running_is_success(world):
    shas, run = world
    code, log, _ = run(shas["later"], shas["later"])
    assert code == 0
    assert "state=success" in _final_state(log)


def test_a_later_commit_that_contains_it_is_success(world):
    shas, run = world
    code, log, _ = run(shas["app_change"], shas["later"])
    assert code == 0
    assert "Live as part of" in _final_state(log)


def test_an_older_commit_running_is_failure(world):
    """The server never moved on: that must not read as live."""
    shas, run = world
    code, log, _ = run(shas["app_change"], shas["base"])
    assert code == 1
    final = _final_state(log)
    assert "state=failure" in final
    assert "rolled back" in final


def test_a_held_deploy_change_is_named_as_the_reason(world):
    shas, run = world
    code, log, _ = run(shas["deploy_change"], shas["app_change"])
    assert code == 1
    assert "held" in _final_state(log)
    assert "--allow-infra" in _final_state(log)


def test_an_unreachable_server_is_named_as_the_reason(world):
    shas, run = world
    code, log, _ = run(shas["later"], "not json at all")
    assert code == 1
    assert "did not answer" in _final_state(log) or "still runs" in _final_state(log)


def test_failed_ci_is_reported_without_waiting(world):
    shas, run = world
    code, log, _ = run(shas["later"], shas["base"], ci="failure")
    assert code == 0
    assert "state=error" in _final_state(log)


def test_a_non_sha_from_the_server_never_reaches_git(world):
    """The /health response is untrusted; an option-looking value must not be passed on."""
    shas, run = world
    code, _log, output = run(shas["later"], "--upload-pack=touch /tmp/pwned")
    assert code == 1
    assert "upload-pack" not in output


# ── Static guarantees ────────────────────────────────────────────────────────

def test_runs_only_for_pushes_to_main_of_this_repo():
    condition = _workflow()["jobs"]["live"]["if"]
    assert "workflow_run.event == 'push'" in condition
    assert "head_branch == 'main'" in condition
    assert "head_repository.full_name == github.repository" in condition


def test_permissions_are_minimal_and_nothing_from_the_trigger_is_checked_out():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read", "statuses": "write"}
    checkout = workflow["jobs"]["live"]["steps"][0]["with"]
    assert checkout["ref"] == "main"
    assert checkout["persist-credentials"] is False


def test_ci_on_main_is_never_cancelled():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"

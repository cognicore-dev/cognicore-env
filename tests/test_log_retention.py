"""Container logs are deleted after 14 days, and the privacy policy says so.

Caddy's access log records visitor IP addresses, so how long logs live is a
promise to users, not just housekeeping. These tests fail if the configured
retention and the published policy drift apart.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOGROTATE = ROOT / "deploy" / "logrotate-docker.conf"
SETUP = ROOT / "deploy" / "setup-host.sh"
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
PRIVACY = ROOT / "PRIVACY.md"

PROMISED_DAYS = 14


def _conf() -> str:
    return LOGROTATE.read_text(encoding="utf-8")


def test_logs_are_dropped_by_age_within_the_promised_window():
    conf = _conf()
    assert "daily" in conf
    maxage = int(re.search(r"^\s*maxage\s+(\d+)", conf, re.MULTILINE).group(1))
    rotate = int(re.search(r"^\s*rotate\s+(\d+)", conf, re.MULTILINE).group(1))
    assert maxage <= PROMISED_DAYS
    # The current file plus `rotate` daily copies is the oldest line that can
    # survive, so it has to stay inside the window the policy promises.
    assert rotate + 1 <= PROMISED_DAYS


def test_docker_keeps_writing_to_the_rotated_file_handle():
    """Without copytruncate, Docker writes to the renamed file and nothing is freed."""
    assert "copytruncate" in _conf()


def test_it_covers_every_container_on_the_host():
    assert "/var/lib/docker/containers/*/*-json.log" in _conf()


def test_setup_installs_it_where_logrotate_looks():
    setup = SETUP.read_text(encoding="utf-8")
    assert "logrotate-docker.conf" in setup
    assert "/etc/logrotate.d/cognicore-docker" in setup


@pytest.mark.parametrize("service", ["mcp", "caddy"])
def test_live_logs_are_capped_between_daily_runs(service: str):
    """A burst must not fill the disk in the hours before logrotate next runs."""
    yaml = pytest.importorskip("yaml")
    options = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"][service]["logging"]["options"]
    assert options["max-size"]
    assert int(options["max-file"]) >= 1


def test_the_policy_states_the_retention_that_is_configured():
    privacy = PRIVACY.read_text(encoding="utf-8")
    assert f"deleted after {PROMISED_DAYS} days" in privacy
    assert "kept by our hosting provider for a limited period" not in privacy

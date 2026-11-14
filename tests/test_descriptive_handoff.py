"""Handoffs say what was done, and a later session's finished work closes earlier tasks.

Replays a real miss: a session planned "add llms-install.md", the next session wrote and
merged it, and Codex, asked about "our latest planned PR", still reported the plan.
"""
import itertools

import pytest

from cognicore.memory import SQLiteMemoryBackend
from cognicore.memory import context_preservation as cp
from cognicore.memory.handoff import build_handoff


@pytest.fixture
def backend(tmp_path, monkeypatch):
    clock = itertools.count(1_794_500_000, 3600)  # one session per hour, in order
    monkeypatch.setattr(cp.time, "time", lambda: float(next(clock)))
    return SQLiteMemoryBackend(str(tmp_path / "mem.db"))


def _done(text):
    return build_handoff([{"role": "assistant", "content": text}], "s", "n", 0.0)["completed"]


@pytest.mark.parametrize("sentence", [
    "Committed and pushed `1bab591` to skill/never-twice-1.0.1.",
    "PR #212 is merged and llms-install.md is on main.",
    "I published never-twice@1.0.0 under @cognicore-dev.",
    "Fixed the Cursor sign-in in oauth.py; 30 OAuth tests passed.",
])
def test_finished_work_is_kept_as_done(sentence):
    text = sentence + " Then I checked the health endpoint at https://mcp.cognicoree.tech/health and it is working."
    assert sentence in _done(text)


@pytest.mark.parametrize("sentence", [
    "I'll push the tag after you merge PR #211.",
    "Once it's merged, the VM deploys commit 29059d9.",
    "The release branch wasn't merged into main yet.",
    "Should I publish 1.0.2 to ClawHub?",
    "Next step: merge fix/cursor-oauth.",
])
def test_plans_questions_and_negations_are_not_done(sentence):
    assert sentence not in _done(sentence + " The health check at https://mcp.cognicoree.tech/health is working.")


def test_the_brief_lists_what_was_done(backend):
    cp.save_session(backend, [
        {"role": "user", "content": "We're working on the Cursor sign-in for the hosted server."},
        {"role": "assistant", "content": (
            "Fixed the redirect allowlist in cognicore/connectors/oauth.py. "
            "Committed `29059d9` on fix/cursor-oauth and pushed it. "
            "Wrote llms-install.md and pushed `d0ab865`. "
            "The server at https://mcp.cognicoree.tech is currently blocked on the deploy.")},
    ], session_name="cursor")
    brief = cp.resume_session(backend)
    assert "Done:" in brief
    assert "29059d9" in brief and "d0ab865" in brief


def _plan_then_finish(backend):
    cp.save_session(backend, [
        {"role": "user", "content": "Our goal is the Cline marketplace submission for cognicore-env."},
        {"role": "assistant", "content": (
            "Next step: add llms-install.md to cognicore-env and open that PR. "
            "We decided to file the Cline listing as an issue because submissions are issues.")},
    ], session_name="mcp-marketplace")
    cp.save_session(backend, [
        {"role": "user", "content": "We're working on the CogniCore releases."},
        {"role": "assistant", "content": (
            "Wrote llms-install.md and pushed `d0ab865`; it merged as PR #212. "
            "Fixed the Cursor sign-in; PR #213 merged. "
            "The heading fix merged as PR #214.")},
    ], session_name="releases")


def test_asking_about_the_latest_pr_gets_the_newest_work(backend):
    _plan_then_finish(backend)
    brief = cp.resume_session(backend, query="what was our latest planned PR")
    assert "Session: releases" in brief
    assert "#212" in brief and "#213" in brief


def test_a_task_finished_in_a_later_session_is_no_longer_open(backend):
    _plan_then_finish(backend)
    # This query matches the older planning session best...
    brief = cp.resume_session(backend, query="Cline marketplace submission issue")
    assert "Session: mcp-marketplace" in brief
    # ...but its llms-install.md task was finished afterwards, so it isn't listed as open.
    assert "Open tasks:" not in brief or "add llms-install.md" not in brief.split("Open tasks:")[1]


def test_an_unfinished_task_stays_open(backend):
    cp.save_session(backend, [
        {"role": "assistant", "content": "Next step: make the 400x400 logo.png for the Cline form."},
    ], session_name="plan")
    cp.save_session(backend, [
        {"role": "assistant", "content": "Wrote llms-install.md and pushed `d0ab865`."},
    ], session_name="later")
    brief = cp.resume_session(backend, query="logo.png Cline form")
    assert "logo.png" in brief.split("Open tasks:")[1]


def test_agents_can_send_done_items_in_a_handoff(backend):
    import json

    handoff = {"goal": "Ship 0.13.0", "completed": ["Released 0.13.0 to PyPI (tag v0.13.0, PR #211)."]}
    cp.save_session(backend, supplied_handoff=json.dumps(handoff), session_name="release")
    assert "Released 0.13.0 to PyPI (tag v0.13.0, PR #211)." in cp.resume_session(backend)

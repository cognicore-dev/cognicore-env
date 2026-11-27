"""Team memory on the hosted server: teams, roles, invites, sharing, recall, audit."""

import sqlite3
import time
from types import SimpleNamespace

import jwt
import pytest

from cognicore.connectors import remote, teams

ALICE, BOB, CAROL, DAN = "github:1", "google:2", "github:3", "google:4"


def _ctx(user_id):
    token = jwt.encode({"sub": user_id, "typ": "access", "exp": int(time.time()) + 600},
                       remote.JWT_SECRET, algorithm=remote.JWT_ALGORITHM)
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"}, query_params={})
    return SimpleNamespace(request_context=SimpleNamespace(request=request, meta=None))


@pytest.fixture(autouse=True)
def hosted(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0")
    monkeypatch.delenv("COGNICORE_DB_PATH", raising=False)
    monkeypatch.setattr(remote, "_team_registry", None)


def team(action, user, **kw):
    return remote.cognicore_team(action, _ctx(user), **kw)


def _code(out):
    return out.split(": ", 1)[1].split()[0]


@pytest.fixture
def acme():
    """Alice owns Acme; Bob is an editor, Carol a viewer; Dan is not a member."""
    team("create", ALICE, name="Acme", your_name="Alice")
    team("join", BOB, code=_code(team("invite", ALICE, team="Acme", role="editor")), your_name="Bob")
    team("join", CAROL, code=_code(team("invite", ALICE, team="Acme", role="viewer")), your_name="Carol")
    return remote._teams().teams_for(ALICE)[0]["team_id"]


# --- teams, invites, roles ------------------------------------------------------------------------

def test_create_invite_join_list_members(acme):
    assert team("list", ALICE) == f"{acme}  Acme  (owner)"
    assert team("list", BOB) == f"{acme}  Acme  (editor)"
    members = team("members", CAROL, team="Acme")
    assert "github:1  Alice  (owner)" in members and "google:2  Bob  (editor)" in members
    assert team("list", DAN) == "No teams."


def test_invites_are_single_use_expire_and_are_stored_hashed(acme, tmp_path, monkeypatch):
    code = _code(team("invite", ALICE, team="Acme"))
    assert "You are in Acme" in team("join", DAN, code=code)
    assert "not valid" in team("join", "github:9", code=code)  # used
    assert "not valid" in team("join", "github:9", code="cct-made-up")
    rows = sqlite3.connect(tmp_path / "remote" / "teams.db").execute("SELECT code_hash FROM invites").fetchall()
    assert all(code not in r[0] for r in rows) and teams._hash(code) in {r[0] for r in rows}
    monkeypatch.setattr(teams, "INVITE_TTL", -1)
    expired = _code(team("invite", ALICE, team="Acme"))
    assert "not valid" in team("join", "github:9", code=expired)


def test_only_owners_invite_or_change_roles(acme):
    assert "needs the owner role" in team("invite", BOB, team="Acme")
    assert "needs the owner role" in team("set_role", BOB, team="Acme", member=CAROL, role="editor")
    assert team("set_role", ALICE, team="Acme", member=CAROL, role="editor") == f"{CAROL} is now editor."


def test_the_last_owner_is_protected(acme):
    assert "at least one owner" in team("set_role", ALICE, team="Acme", member=ALICE, role="editor")
    assert "last owner cannot leave" in remote.cognicore_team_remove("Acme", _ctx(ALICE))
    team("set_role", ALICE, team="Acme", member=BOB, role="owner")
    assert remote.cognicore_team_remove("Acme", _ctx(ALICE)) == "Left Acme."
    assert team("list", ALICE) == "No teams."


def test_remove_member_and_delete_team(acme, tmp_path):
    assert "needs the owner role" in remote.cognicore_team_remove("Acme", _ctx(BOB), member=CAROL)
    assert remote.cognicore_team_remove("Acme", _ctx(ALICE), member=CAROL) == f"Removed {CAROL} from Acme."
    remote.cognicore_team("share", _ctx(BOB), team="Acme", text="Release on Friday")
    db = remote._teams()._db_path(acme)
    assert db.exists()
    assert remote.cognicore_team_remove("Acme", _ctx(ALICE), delete_team=True).startswith("Deleted team Acme")
    assert not db.exists() and team("list", BOB) == "No teams."


def test_ambiguous_team_names_need_the_id(acme):
    team("create", ALICE, name="acme")
    out = team("members", ALICE, team="ACME")
    assert "several teams named" in out
    assert "github:1" in team("members", ALICE, team=acme)


# --- shared memory ----------------------------------------------------------------------------

def test_share_needs_text():
    assert remote.cognicore_team("share", _ctx(ALICE), team="Acme", text="  ") == "Error: give the text to share."


def test_editors_share_viewers_and_outsiders_cannot(acme):
    out = remote.cognicore_team("share", _ctx(BOB), team="Acme", text="The staging password rotates on Monday")
    assert out.startswith("Shared 1 fact(s) with Acme (ids=team:")
    assert "needs the owner or editor role" in remote.cognicore_team("share", _ctx(CAROL), team="Acme", text="x")
    assert teams.NOT_FOUND in remote.cognicore_team("share", _ctx(DAN), team="Acme", text="x")
    assert teams.NOT_FOUND in remote.cognicore_team("share", _ctx(DAN), team=acme, text="x")  # the id does not help


def test_secrets_are_redacted_before_sharing(acme):
    out = remote.cognicore_team("share", _ctx(BOB), team="Acme",
                                text="Deploy key is sk-test-FAKE0000000000000000000000 for staging")
    assert "secret(s) redacted" in out
    hit = remote._teams().backend(acme).search("deploy key staging", top_k=1)[0]
    assert "sk-test-FAKE" not in hit.entry.text


def test_recall_includes_team_memory_labelled_within_the_same_top_k(acme):
    remote.cognicore_team("share", _ctx(BOB), team="Acme", text="Billing retries failed charges three times")
    for i in range(6):
        remote.cognicore_remember(f"Billing note number {i}", _ctx(ALICE))
    out = remote.cognicore_recall("billing retries", _ctx(ALICE), top_k=3)
    assert "Found 3 memories:" in out and out.count("\n  [") == 3
    assert "(team Acme, shared by Bob" in out and f"#team:{acme}:" in out
    assert "Billing retries" not in remote.cognicore_recall("billing retries", _ctx(DAN))
    assert "team Acme" not in remote.cognicore_recall("billing retries", _ctx(ALICE), scope="user")


def test_forget_follows_roles(acme):
    def share(user, text):
        return remote.cognicore_team("share", _ctx(user), team="Acme", text=text).split("ids=")[1].rstrip(")")

    bob_id = share(BOB, "Bob's fact about invoices")
    alice_id = share(ALICE, "Alice's fact about invoices")
    assert "only the memories they shared" in remote.cognicore_forget(alice_id, _ctx(BOB))
    assert teams.NOT_FOUND in remote.cognicore_forget(bob_id, _ctx(DAN))
    assert "needs the owner or editor role" in remote.cognicore_forget(bob_id, _ctx(CAROL))
    assert remote.cognicore_forget(bob_id, _ctx(BOB)) == "OK"
    assert remote.cognicore_forget(alice_id, _ctx(ALICE)) == "OK"
    assert remote.cognicore_forget(alice_id, _ctx(ALICE)) == "Not found"


def test_audit_log_records_who_did_what(acme):
    remote.cognicore_team("share", _ctx(BOB), team="Acme", text="A shared fact")
    log = team("audit", CAROL, team="Acme")
    for actor, action in ((ALICE, "create"), (ALICE, "invite"), (BOB, "join"), (CAROL, "join"), (BOB, "share")):
        assert any(actor in line and f"  {action}  " in line for line in log.splitlines()), (actor, action)
    assert teams.NOT_FOUND in team("audit", DAN, team="Acme")


def test_limits(acme, monkeypatch):
    monkeypatch.setattr(teams, "MAX_MEMBERS", 3)
    assert "already has 3 members" in team("join", DAN, code=_code(team("invite", ALICE, team="Acme")))
    monkeypatch.setattr(teams, "MAX_OWNED_TEAMS", 1)
    assert "already created 1 teams" in team("create", ALICE, name="Second")


def test_local_mode_has_no_teams(monkeypatch, tmp_path):
    monkeypatch.setenv("COGNICORE_DB_PATH", str(tmp_path / "local.db"))
    assert "hosted server" in remote.cognicore_team("list", _ctx(ALICE))
    assert "hosted server" in remote.cognicore_team("share", _ctx(ALICE), team="Acme", text="x")


def test_unknown_action():
    assert team("bogus", ALICE).startswith("Error: action must be")


def test_recall_without_teams_is_unchanged(monkeypatch):
    for i in range(4):
        remote.cognicore_remember(f"Billing note number {i}", _ctx(ALICE))
    before = remote.cognicore_recall("billing note", _ctx(ALICE), top_k=3)
    monkeypatch.setattr(remote, "_authenticated_user", lambda ctx: None)  # as if teams did not exist
    assert remote.cognicore_recall("billing note", _ctx(ALICE), top_k=3) == before


def test_team_tools_are_opt_in():
    """Their definitions would cost every hosted user tokens on every turn; operators switch them on."""
    import asyncio

    listed = {t.name for t in asyncio.run(remote.mcp.list_tools())}
    assert "teams" in remote.OPT_IN_TOOLSETS
    assert not {"cognicore_team", "cognicore_team_remove"} & listed
    assert "cognicore_remember" in listed  # sharing to a team uses the existing tool's team argument


def test_everyday_tools_carry_nothing_about_teams_so_people_without_teams_pay_nothing():
    """Team memory adds no argument and no wording to remember, recall or forget: no extra tokens per turn."""
    import asyncio
    import json

    tools = {t.name: t for t in asyncio.run(remote.mcp.list_tools())}
    for name in ("cognicore_remember", "cognicore_recall", "cognicore_forget"):
        definition = json.dumps({"description": tools[name].description, "schema": tools[name].inputSchema}).lower()
        assert "team" not in definition, name

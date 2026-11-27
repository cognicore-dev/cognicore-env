"""File cards: agents keep what they learned about a project's files across sessions."""

import io
import json

import pytest

from cognicore import file_cards as fc
from cognicore import hooks
from cognicore.memory.context_preservation import TokenBudget
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend

BILLING = '''"""Billing."""
RATE = 0.19


class Invoice:
    def total(self):
        return 1

    def tax(self):
        return 2


def format_eur(amount):
    return f"{amount} EUR"
'''


def _line(kind, content):
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}})


def _claude_transcript(path, cwd, reads=(), edits=(), text="Looked at the billing code."):
    blocks = [{"type": "tool_use", "name": "Read", "input": {"file_path": f"{cwd}/{r}"}} for r in reads]
    blocks += [{"type": "tool_use", "name": "Edit", "input": {"file_path": f"{cwd}/{e}", "old_string": "x"}}
               for e in edits]
    blocks.append({"type": "text", "text": text})
    path.write_text("\n".join([_line("user", "Fix the EUR formatting in billing."), _line("assistant", blocks)])
                    + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "billing-app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "billing.py").write_text(BILLING, encoding="utf-8")
    (root / "README.md").write_text("# Billing app\n\n## Setup\n\nRun it.\n\n## Deploy\n", encoding="utf-8")
    (root / ".env").write_text("STRIPE_KEY=sk_live_abc\n", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")
    return root


@pytest.fixture
def backend(tmp_path):
    return SQLiteMemoryBackend(str(tmp_path / "m.db"))


# --- outlines -------------------------------------------------------------------------------------

def test_python_outline_has_names_and_lines_not_values():
    out = fc.outline("src/billing.py", BILLING)
    assert out == ["5: class Invoice", "6:   def total", "9:   def tax", "13: def format_eur"]
    assert not any("0.19" in o for o in out)


@pytest.mark.parametrize("rel,text,expected", [
    ("a.ts", "export async function load(x) {\n}\nexport class Store {}\nexport const save = async (y) => 1\n"
             "interface Props {}\n", ["1: export async function load", "3: export class Store",
                                      "4: export const save", "5: interface Props"]),
    ("a.go", "package main\nfunc (s *S) Run() {}\nfunc helper() {}\n", ["2: func", "3: func helper"]),
    ("a.rs", "pub fn build() {}\nstruct Config {}\n", ["1: pub fn build", "2: struct Config"]),
    ("doc.md", "# Title\ntext\n## Part\n", ["1: # Title", "3: ## Part"]),
    ("c.json", '{"name": "app", "token": "s3cret"}', ["key name", "key token"]),
    ("c.yaml", "server:\n  port: 8080\nauth:\n  key: s3cret\n", ["1: server", "3: auth"]),
    ("c.toml", "[tool.ruff]\nline-length = 120\n", ["1: [tool.ruff]", "2: line-length"]),
])
def test_outlines_for_other_files_keep_only_names(rel, text, expected):
    out = fc.outline(rel, text)
    assert out[:len(expected)] == expected or out == expected
    assert not any("s3cret" in o or "8080" in o for o in out)


def test_long_outlines_are_cut():
    text = "".join(f"def f{i}():\n    pass\n" for i in range(60))
    out = fc.outline("m.py", text)
    assert len(out) == fc.MAX_OUTLINE + 1 and out[-1] == "... 20 more"


# --- what a session used ---------------------------------------------------------------------

def test_files_used_in_a_claude_session(tmp_path, project):
    t = _claude_transcript(tmp_path / "t.jsonl", project, reads=["src/billing.py", "README.md", "../outside.txt"],
                           edits=["src/billing.py"])
    assert fc.files_used(t, project) == {"src/billing.py": {"reads": 1, "edits": 1},
                                         "README.md": {"reads": 1, "edits": 0}}


def test_files_used_in_a_codex_session(tmp_path, project):
    rows = [{"type": "session_meta", "payload": {"cwd": str(project)}}]
    for cmd in (["bash", "-lc", "sed -n '1,80p' src/billing.py"], ["bash", "-lc", "cat README.md | head"],
                ["powershell", "-Command", "Get-Content -Raw src/billing.py"]):
        rows.append({"type": "response_item", "payload": {"type": "local_shell_call", "action": {"command": cmd}}})
    rows.append({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch",
                 "input": "*** Begin Patch\n*** Update File: src/billing.py\n@@\n*** End Patch"}})
    t = tmp_path / "rollout.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    import os
    old = os.getcwd()
    os.chdir(project)  # Codex commands use paths relative to its working folder
    try:
        used = fc.files_used(str(t), project)
    finally:
        os.chdir(old)
    assert used["src/billing.py"] == {"reads": 2, "edits": 1}
    assert used["README.md"]["reads"] == 1


# --- cards ----------------------------------------------------------------------------------------

def test_capture_counts_sessions_not_saves_and_skips_secret_and_binary(tmp_path, backend, project):
    t = _claude_transcript(tmp_path / "t.jsonl", project, reads=["src/billing.py", ".env", "logo.png"])
    assert fc.capture(backend, t, str(project), "claude-code:s1") == 1
    assert fc.capture(backend, t, str(project), "claude-code:s1") == 1  # the same session saved again
    t2 = _claude_transcript(tmp_path / "t2.jsonl", project, reads=["src/billing.py"])
    fc.capture(backend, t2, str(project), "claude-code:s2")
    card = fc.card(backend, str(project), "src/billing.py")
    assert card["sessions"] == 2 and card["lines"] == 14 and "5: class Invoice" in card["outline"]
    assert not card["changed_since_last_session"]
    assert fc.card(backend, str(project), ".env") is None
    assert fc.card(backend, str(project), "logo.png") is None


def test_changed_file_is_flagged_and_its_outline_rebuilt(tmp_path, backend, project):
    fc.capture(backend, _claude_transcript(tmp_path / "t.jsonl", project, reads=["src/billing.py"]),
               str(project), "s1")
    (project / "src" / "billing.py").write_text(BILLING + "\n\ndef refund(x):\n    return x\n", encoding="utf-8")
    card = fc.card(backend, str(project), str(project / "src" / "billing.py"))  # absolute paths work too
    assert card["changed_since_last_session"] and "17: def refund" in card["outline"]
    assert not fc.card(backend, str(project), "src/billing.py")["changed_since_last_session"]


def test_notes_are_kept_and_redacted(tmp_path, backend, project):
    fc.capture(backend, _claude_transcript(tmp_path / "t.jsonl", project, reads=["src/billing.py"]),
               str(project), "s1")
    stored = fc.set_note(backend, str(project), "src/billing.py",
                         "Rounding lives in format_eur. Test key sk-test-FAKE0000000000000000000000")
    assert stored.startswith("Rounding lives in format_eur.") and "sk-test-FAKE" not in stored
    assert fc.card(backend, str(project), "src/billing.py")["note"] == stored
    assert fc.set_note(backend, str(project), "missing.py", "x") is None


def test_brief_block_fits_its_budget_and_drops_deleted_files(tmp_path, backend, project):
    fc.capture(backend, _claude_transcript(tmp_path / "t.jsonl", project, reads=["src/billing.py", "README.md"]),
               str(project), "s1")
    block = fc.brief_block(backend, str(project), 150)
    assert block.startswith("Files used in earlier sessions") and "src/billing.py (14 lines, 1 session)" in block
    assert TokenBudget.estimate_tokens(block) <= 150
    assert fc.brief_block(backend, str(project), 20) == ""
    (project / "README.md").unlink()
    assert "README.md" not in fc.brief_block(backend, str(project), 150)


def test_switched_off(tmp_path, backend, project, monkeypatch):
    monkeypatch.setenv("COGNICORE_FILE_CARDS", "0")
    t = _claude_transcript(tmp_path / "t.jsonl", project, reads=["src/billing.py"])
    assert fc.capture(backend, t, str(project), "s1") == 0
    assert fc.brief_block(backend, str(project)) == ""


# --- through the hooks ------------------------------------------------------------------------

def _run(command, event, monkeypatch, data_dir):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(data_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hooks.main([command]) == 0
    return out.getvalue()


def test_save_then_next_session_starts_with_the_files(tmp_path, project, monkeypatch):
    t = _claude_transcript(tmp_path / "t.jsonl", project, reads=["src/billing.py"], edits=["src/billing.py"],
                           text="We decided to keep rounding in format_eur because finance needs one place.")
    _run("save", {"session_id": "s-1", "transcript_path": t, "cwd": str(project)}, monkeypatch, tmp_path / "mem")
    out = json.loads(_run("session-start", {"cwd": str(project), "source": "startup"}, monkeypatch, tmp_path / "mem"))
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "src/billing.py (14 lines, 1 session)" in context and "13: def format_eur" in context
    budget = 700
    assert TokenBudget.estimate_tokens(context) <= budget + 5  # the brief keeps its budget


def test_brief_never_grows_past_its_budget(tmp_path, project, monkeypatch):
    monkeypatch.setenv("COGNICORE_RESUME_TOKENS", "120")
    t = _claude_transcript(tmp_path / "t.jsonl", project, reads=["src/billing.py"],
                           text="We decided to keep rounding in format_eur because finance needs one place.")
    _run("save", {"session_id": "s-1", "transcript_path": t, "cwd": str(project)}, monkeypatch, tmp_path / "mem")
    out = json.loads(_run("session-start", {"cwd": str(project), "source": "startup"}, monkeypatch, tmp_path / "mem"))
    context = out["hookSpecificOutput"]["additionalContext"]
    brief_alone = context.split("\n\nFiles used in earlier sessions")[0]
    assert TokenBudget.estimate_tokens(context) <= max(120, TokenBudget.estimate_tokens(brief_alone))


def test_file_card_tools_are_opt_in_and_work(tmp_path, project, monkeypatch):
    pytest.importorskip("mcp")
    from cognicore.mcp.server import create_mcp_server

    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("COGNICORE_TOOLSETS", "search,project")  # the plugin's default: no per-turn cost
    assert "cognicore_file_card" not in create_mcp_server()._tool_manager._tools
    monkeypatch.setenv("COGNICORE_TOOLSETS", "search,project,files")
    tools = create_mcp_server()._tool_manager._tools
    card = json.loads(tools["cognicore_file_card"].fn(path="src/billing.py", cwd=str(project)))
    assert card["lines"] == 14 and "13: def format_eur" in card["outline"]
    noted = json.loads(tools["cognicore_note_file"].fn(path="src/billing.py", note="EUR format lives here",
                                                       cwd=str(project)))
    assert noted == {"stored": "EUR format lives here"}
    assert "error" in json.loads(tools["cognicore_file_card"].fn(path=".env", cwd=str(project)))

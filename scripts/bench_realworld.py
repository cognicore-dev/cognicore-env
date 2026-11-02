"""Real-world memory benchmark: session 1 does work, a fresh session 2 must continue it.

    python scripts/bench_realworld.py <arm> [task ...] [--copy-codex-auth]

Arms (session 1 always runs in Claude Code; session 2 in Claude Code unless noted):
    baseline              no memory
    cognicore_auto        CogniCore's plugin (plugins/cognicore): hooks + local server
    cognicore             CogniCore tools only; the agent is told to save and resume
    claudemem             claude-mem (install it first: npx claude-mem install)
    x_cognicore           session 2 in Codex with CogniCore's MCP server + AGENTS.md rule
    x_cognicore_hooksim   as x_cognicore, plus CogniCore's real hooks run where Codex would
    x_claudemem_hooksim   session 2 in Codex with claude-mem's Codex hook output + its MCP server
Tasks: billing, retry, export (default: all three).

Per task: session 1 is told facts and defers a follow-up. Its transcript is then moved
out of ~/.claude/projects, so only the memory system can carry anything over. A cold
session 2 must finish the follow-up and answer 8 questions. Results (the agents' own
token usage, answers, scores, follow-up check) go to $BENCH_WORKDIR/results/<arm>/.

Every run is a real agent session on your Claude plan or API quota (and Codex quota for
x_* arms). Hooks are simulated in Codex arms because Codex runs hooks only after a person
trusts them (/hooks). Settings:
    CLAUDE_BIN, CODEX_BIN   agent binaries (default: PATH, then known install folders)
    BENCH_WORKDIR           work folder (default: .bench_realworld in the repo)
    BENCH_CROSS_DIR         repos for Codex arms. Keep it OUTSIDE your home folder, or Codex
                            loads ~/.codex/config.toml as project config.
    BENCH_MODEL             Claude model for both sessions (default: sonnet)
    CLAUDEMEM_PLUGIN        claude-mem plugin folder (default: newest in ~/.claude/plugins/cache)
    --copy-codex-auth       copy ~/.codex/auth.json into the throwaway Codex homes for the run;
                            it is deleted again when the run ends
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_EDITOR_GLOBS = [f".{editor}/extensions/anthropic.claude-code-*/resources/native-binary/claude*"
                 for editor in ("vscode", "cursor", "antigravity-ide", "windsurf")]


def _find(env: str, names, globs=()) -> str:
    """An agent binary: $ENV, then PATH, then the newest match in known install folders."""
    if os.environ.get(env):
        return os.environ[env]
    for name in names:
        if shutil.which(name):
            return shutil.which(name)
    for pattern in globs:
        found = sorted(Path.home().glob(pattern))  # editor extensions auto-update; take the newest
        if found:
            return str(found[-1])
    return ""


CLAUDE = _find("CLAUDE_BIN", ["claude"], _EDITOR_GLOBS)
CODEX = _find("CODEX_BIN", ["codex"], [".codex/plugins/.plugin-appserver/codex*"])
VENV_PY = sys.executable
COGNICORE_REPO = str(REPO)
HERE = Path(os.environ.get("BENCH_WORKDIR") or REPO / ".bench_realworld")
MODEL = os.environ.get("BENCH_MODEL", "sonnet")

TASKS = {
    "billing": {
        "files": {
            "billing.py": '''TAX_RATES = {"DE": 0.19, "FR": 0.20, "NL": 0.21}


def total_with_tax(net: float, country: str) -> float:
    return round(net * (1 + TAX_RATES[country]), 2)


def format_eur(amount: float) -> str:
    return f"{amount:.2f} EUR"
''',
            "tests/__init__.py": "",
            "tests/test_billing.py": '''import unittest

from billing import total_with_tax


class TestTotals(unittest.TestCase):
    def test_fr(self):
        self.assertEqual(total_with_tax(100, "FR"), 120.0)


if __name__ == "__main__":
    unittest.main()
''',
        },
        "session1": (
            "Ticket BILL-311: customers in Germany see invoice totals that are off by one cent. "
            "We decided to compute totals with decimal.Decimal and ROUND_HALF_UP instead of round(), "
            "because finance requires commercial rounding. Fix total_with_tax in billing.py and add a test "
            "for the DE case in tests/test_billing.py (run it with python -m unittest). "
            "Do NOT change format_eur in this session: that is a follow-up for next time. It must produce "
            "German formatting with a comma decimal separator, like '12,34 €'. "
            "FYI the staging deploy is on Thursday and the reviewer is Anika."
        ),
        "questions": {
            "q1": ("What is the ticket id?", ["bill-311"], "conv"),
            "q2": ("Which rounding mode was chosen?", ["round_half_up", "half_up", "half up"], "code"),
            "q3": ("Why was round() not used?", ["commercial rounding", "finance"], "conv"),
            "q4": ("What exact output format must format_eur produce for 12.34?", ["12,34 €"], "conv"),
            "q5": ("Who is the reviewer?", ["anika"], "conv"),
            "q6": ("On which day is the staging deploy?", ["thursday"], "conv"),
            "q7": ("Which test file holds the DE test?", ["tests/test_billing.py", "test_billing.py"], "code"),
            "q8": ("Which country had the wrong totals?", ["germany", "de"], "conv"),
        },
        "done_check": "from billing import format_eur; assert format_eur(12.34) == '12,34 €', format_eur(12.34)",
    },
    "retry": {
        "files": {
            "client.py": '''import json
import urllib.request

BASE_URL = "https://api.billing.internal/v2"


def fetch_invoice(invoice_id: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}/invoices/{invoice_id}") as resp:
        return json.loads(resp.read())
''',
        },
        "session1": (
            "Incident INC-7781: fetch_invoice in client.py fails hard when the invoice API has a blip. "
            "Add retries. Decisions: at most 3 attempts, exponential backoff with a 0.5 second base, and retry "
            "only on HTTP 502, 503 and 504, never on 4xx, because a 4xx means our request is wrong and retrying "
            "just hammers the API. Read the request timeout from the env var INVOICE_API_TIMEOUT, default 10 "
            "seconds. Leave jitter out for now: the follow-up for next session is to add +/-20% random jitter "
            "to the backoff delay. The on-call owner for this API is Marek."
        ),
        "questions": {
            "q1": ("What is the incident id?", ["inc-7781"], "conv"),
            "q2": ("Maximum number of attempts?", ["3"], "code"),
            "q3": ("Backoff base in seconds?", ["0.5"], "code"),
            "q4": ("Which HTTP status codes are retried?", ["502"], "code"),
            "q5": ("Why are 4xx errors never retried?", ["request is wrong", "hammer", "our request"], "conv"),
            "q6": ("Name of the timeout env var and its default?", ["invoice_api_timeout"], "code"),
            "q7": ("What follow-up was left for this session, with its exact amount?", ["20%", "20 %", "0.2"], "conv"),
            "q8": ("Who is the on-call owner?", ["marek"], "conv"),
        },
        "done_check": "import re, pathlib; s = pathlib.Path('client.py').read_text(); assert re.search(r'random|uniform|jitter', s), 'no jitter'",
    },
    "export": {
        "files": {
            "export.py": '''import csv
import re


def sanitize(value: str) -> str:
    return re.compile(r"[\\r\\n\\t]+").sub(" ", value).strip()


def to_csv_row(record: dict) -> list:
    return [sanitize(str(record[k])) for k in ("id", "customer", "amount", "note")]


def export(records, path):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        for record in records:
            writer.writerow(to_csv_row(record))
''',
        },
        "session1": (
            "The nightly export job got slow: 14 minutes on 2026-10-29, versus 3 minutes before. I profiled it: "
            "80% of the time is in to_csv_row, which is called 1.2 million times, and the culprit is that "
            "sanitize() recompiles its regex on every call. Fix sanitize by precompiling the regex at module "
            "level. We considered switching to pandas and rejected it, because the job runs on Lambda with a "
            "250 MB package limit. Do NOT batch the writes yet: the follow-up for next session is to replace the "
            "per-row writerow loop with a single writer.writerows call. The job owner is Ravi."
        ),
        "questions": {
            "q1": ("How long did the job take on the slow night, and before?", ["14"], "conv"),
            "q2": ("On which date was it slow?", ["2026-10-29"], "conv"),
            "q3": ("What share of time was in to_csv_row?", ["80"], "conv"),
            "q4": ("How many times is to_csv_row called?", ["1.2 million", "1.2m", "1,200,000", "1200000"], "conv"),
            "q5": ("What alternative was rejected?", ["pandas"], "conv"),
            "q6": ("Why was it rejected?", ["250"], "conv"),
            "q7": ("Who owns the job?", ["ravi"], "conv"),
            "q8": ("What was the root cause of the slowdown?", ["recompil", "compil"], "code"),
        },
        "done_check": "import pathlib; s = pathlib.Path('export.py').read_text(); assert 'writerows' in s, 'no writerows'",
    },
}

SESSION2 = (
    "Continue where the previous session on this repository left off, and finish the follow-up work that was "
    "deferred to this session. Don't ask me questions; work from whatever you can find. When you are done, end "
    "your reply with a line 'ANSWERS:' followed by one line of JSON answering these questions (use null if you "
    "don't know): {questions}"
)

ARM_PROMPT = {
    "baseline": None,
    "claudemem": None,  # automatic via its hooks
    "cognicore_auto": None,  # automatic via the cognicore-memory plugin hooks
    # Same guidance the CogniCore plugin skill gives in real use.
    "cognicore": (
        "You have CogniCore memory tools. At the start of a session, call cognicore_resume_session with a query "
        "describing the task. Before you finish, call cognicore_save_session with handoff_json (goal, "
        "current_state, decisions with why, open_tasks, key_facts, files), keeping ids, versions, names and "
        "numbers verbatim."
    ),
}


def data_dir_for(arm: str) -> Path:
    return HERE / ("cc_auto_data" if arm == "cognicore_auto" else "cc_data")


def mcp_config(arm: str, out: Path) -> Path:
    servers = {}
    if arm in ("cognicore", "cognicore_auto"):
        servers["cognicore"] = {
            "command": VENV_PY, "args": ["-m", "cognicore.mcp.server"],
            "env": {"COGNICORE_DATA_DIR": str(data_dir_for(arm)), "PYTHONPATH": COGNICORE_REPO},
        }
    if arm == "claudemem":  # the plugin's own .mcp.json server, which strict MCP mode would otherwise drop
        servers["mcp-search"] = {"type": "stdio", "command": "node",
                                 "args": [str(CLAUDEMEM_PLUGIN / "scripts" / "mcp-server.cjs")]}
    out.write_text(json.dumps({"mcpServers": servers}))
    return out


def run_claude(arm: str, cwd: Path, prompt: str) -> dict:
    cmd = [CLAUDE, "-p", prompt, "--model", MODEL, "--output-format", "json",
           "--strict-mcp-config", "--mcp-config", str(mcp_config(arm, HERE / f"mcp_{arm}.json")),
           "--permission-mode", "acceptEdits", "--max-turns", "40",
           "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep", "Bash(python:*)", "Bash(python3:*)",
           "Bash(ls:*)", "Bash(cat:*)", "mcp__cognicore__cognicore_resume_session",
           "mcp__cognicore__cognicore_save_session", "mcp__cognicore__cognicore_recall", "mcp__mcp-search"]
    # No user settings in any arm, so none of the user's other plugins load. claude-mem is
    # loaded on its own for its arm (hooks, skills and MCP server, as a real install has).
    cmd += ["--setting-sources", "project"]
    if arm == "claudemem":
        cmd += ["--plugin-dir", str(CLAUDEMEM_PLUGIN)]
    if arm == "cognicore_auto":  # the real plugin: hooks + skill (its hosted .mcp.json is ignored in strict mode)
        cmd += ["--plugin-dir", str(Path(COGNICORE_REPO) / "plugins" / "cognicore")]
    if ARM_PROMPT[arm]:
        cmd += ["--append-system-prompt", ARM_PROMPT[arm]]
    env = dict(os.environ)
    # claude-mem's hooks call its launcher, and its observer drives the claude CLI.
    env["PATH"] = os.pathsep.join([str(Path(CLAUDE).parent), str(Path.home() / "AppData/Local/claude-mem/bin"),
                                   env.get("PATH", "")])
    if arm == "cognicore_auto":  # the hooks run in this Python, against the same memory as the MCP server
        env.update(COGNICORE_PYTHON=VENV_PY.replace("\\", "/"), COGNICORE_DATA_DIR=str(data_dir_for(arm)),
                   PYTHONPATH=COGNICORE_REPO)
    t = time.time()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          stdin=subprocess.DEVNULL, timeout=1800, env=env)
    wall = round(time.time() - t, 1)
    try:
        res = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": proc.stdout[-2000:] + proc.stderr[-2000:], "wall_s": wall}
    u = res.get("usage", {})
    return {
        "wall_s": wall, "num_turns": res.get("num_turns"), "cost_usd": res.get("total_cost_usd"),
        "input_tokens": u.get("input_tokens", 0), "cache_creation": u.get("cache_creation_input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
        "total_input": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
        "result": res.get("result", ""), "is_error": res.get("is_error"), "session_id": res.get("session_id"),
    }


CLAUDEMEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
CLAUDEMEM_PLUGIN = Path(os.environ.get("CLAUDEMEM_PLUGIN") or (sorted(
    (Path.home() / ".claude" / "plugins" / "cache" / "thedotmack" / "claude-mem").glob("[0-9]*")) or [Path("")])[-1])


def wait_for_claudemem(session_id, timeout=600):
    """Wait until claude-mem has summarized the session and has nothing queued for it."""
    import sqlite3

    # In headless runs it writes per-tool observations but no session summary, so "done" means:
    # nothing queued for the session and the observation count unchanged for 60 s.
    start, last_count, stable_since = time.time(), -1, None
    while time.time() - start < timeout:
        try:
            c = sqlite3.connect(f"file:{CLAUDEMEM_DB}?mode=ro", uri=True, timeout=5)
            row = c.execute("SELECT memory_session_id FROM sdk_sessions WHERE content_session_id = ?",
                            (session_id,)).fetchone()
            count = c.execute("SELECT COUNT(*) FROM observations WHERE memory_session_id = ?",
                              (row[0] if row else "",)).fetchone()[0]
            pending = c.execute("SELECT COUNT(*) FROM pending_messages WHERE content_session_id = ? "
                                "AND status NOT IN ('processed', 'done', 'failed')", (session_id,)).fetchone()[0]
            c.close()
            if row and not pending and count == last_count:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= 60:
                    return round(time.time() - start, 1)
            else:
                stable_since = None
            last_count = count
        except sqlite3.Error:
            pass
        time.sleep(10)
    return None  # timed out: recorded so the result can be flagged


def quarantine_transcripts(session_id, label):
    """Move session 1's Claude Code transcript out of ~/.claude/projects.

    Otherwise session 2 can find and read the raw transcript, which measures
    "can the agent dig up old logs" rather than the memory system under test.
    """
    if not session_id:
        return
    for f in (Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"):
        dest = HERE / "quarantine" / f"{label}-{int(time.time())}"  # unique: tasks get re-run
        dest.mkdir(parents=True, exist_ok=True)
        for item in f.parent.iterdir():
            shutil.move(str(item), str(dest / item.name))



# Cross-agent arms: session 1 in Claude Code (that system's normal setup), session 2 in Codex.
CROSS = {"x_cognicore": "cognicore_auto", "x_cognicore_hooksim": "cognicore_auto", "x_claudemem_hooksim": "claudemem"}
# Outside the home folder: under it, Codex loads ~/.codex/config.toml as project config.
CROSS_REPOS = Path(os.environ.get("BENCH_CROSS_DIR") or (
    Path(os.path.abspath(os.sep)) / "cognicore_bench" if os.name == "nt" else Path("/tmp/cognicore_bench"))) / "repos"


def prepare_codex_home(arm: str, copy_auth: bool) -> Path:
    """A throwaway Codex home holding only the system under test (no user plugins or connectors)."""
    home = codex_home(arm)
    home.mkdir(parents=True, exist_ok=True)
    lines = ['[windows]', 'sandbox = "elevated"', ''] if os.name == "nt" else []
    if arm in ("x_cognicore", "x_cognicore_hooksim"):
        lines += ['[mcp_servers.cognicore]', f'command = {json.dumps(VENV_PY)}', 'args = ["-m", "cognicore.mcp.server"]',
                  'default_tools_approval_mode = "approve"', '', '[mcp_servers.cognicore.env]',
                  f'COGNICORE_DATA_DIR = {json.dumps(str(data_dir_for("cognicore_auto")))}',
                  f'PYTHONPATH = {json.dumps(COGNICORE_REPO)}']
        from cognicore.agent_setup import setup_codex
        setup_codex(str(home), hooks=False)  # the AGENTS.md rule; hooks are simulated (they need /hooks trust)
    elif arm == "x_claudemem_hooksim":
        lines += ['[mcp_servers.claude-mem]', 'command = "node"',
                  f'args = [{json.dumps(str(CLAUDEMEM_PLUGIN / "scripts" / "mcp-server.cjs"))}]',
                  'default_tools_approval_mode = "approve"']
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    auth = Path.home() / ".codex" / "auth.json"
    if copy_auth and auth.exists():
        shutil.copy2(auth, home / "auth.json")
    return home


def codex_home(arm: str) -> Path:
    # The simulated-hooks arm uses the same Codex setup as x_cognicore (MCP server + AGENTS.md rule).
    return HERE / "codex_homes" / arm


def run_codex(arm: str, cwd: Path, prompt: str, developer_context: str = "") -> dict:
    """Run one headless Codex session; usage comes from its own turn.completed events."""
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home(arm))
    env["PATH"] = os.pathsep.join([str(Path(CLAUDE).parent), str(Path.home() / "AppData/Local/claude-mem/bin"),
                                   env.get("PATH", "")])
    # --disable apps: no ChatGPT-account connectors (the user's hosted CogniCore is one of them).
    cmd = [CODEX, "exec", "--json", "--skip-git-repo-check", "-C", str(cwd),
           "-s", "workspace-write", "-c", 'approval_policy="never"', "--disable", "apps"]
    if arm != "x_cognicore_hooksim":
        cmd.append("--ephemeral")  # the simulated-hooks arm keeps its session log for the save hook
    if developer_context:
        # Where Codex puts a SessionStart hook's additionalContext: developer context.
        cmd += ["-c", "developer_instructions=" + json.dumps(developer_context)]
    cmd.append(prompt)
    t = time.time()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          stdin=subprocess.DEVNULL, timeout=1800, env=env)
    wall = round(time.time() - t, 1)
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    final, tools, errors, turns = "", [], [], 0
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "turn.completed":
            turns += 1
            for k in usage:
                usage[k] += (ev.get("usage") or {}).get(k, 0) or 0
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed":
            if item.get("type") == "agent_message":
                final = item.get("text", final)
            elif item.get("type") in ("mcp_tool_call", "command_execution", "file_change"):
                tools.append(item.get("tool") or item.get("command") or item.get("type"))
        if ev.get("type") in ("error", "turn.failed"):
            errors.append(json.dumps(ev)[:300])
    return {
        "agent": "codex", "wall_s": wall, "num_turns": turns, "cost_usd": None,
        "input_tokens": usage["input_tokens"] - usage["cached_input_tokens"], "cache_creation": 0,
        "cache_read": usage["cached_input_tokens"], "output_tokens": usage["output_tokens"],
        "total_input": usage["input_tokens"], "result": final, "tools": tools, "errors": errors,
        "returncode": proc.returncode, "stderr_tail": proc.stderr[-1500:],
    }



def run_hook(command: str, event: dict, agent: str = "codex") -> str:
    """Run CogniCore's real hook exactly as Codex would: event JSON on stdin, output on stdout."""
    env = dict(os.environ, COGNICORE_DATA_DIR=str(data_dir_for("cognicore_auto")), PYTHONPATH=COGNICORE_REPO)
    proc = subprocess.run([VENV_PY, "-m", "cognicore.hooks", command, "--agent", agent], input=json.dumps(event),
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    return proc.stdout



def claudemem_codex_context(event: dict) -> str:
    """Run claude-mem's Codex SessionStart hook core (from its hooks/codex-hooks.json) and return its context."""
    env = dict(os.environ, CLAUDE_MEM_CODEX_HOOK="1")
    env["PATH"] = os.pathsep.join([str(Path(CLAUDE).parent), str(Path.home() / "AppData/Local/claude-mem/bin"),
                                   env.get("PATH", "")])
    scripts = CLAUDEMEM_PLUGIN / "scripts"
    proc = subprocess.run(["node", str(scripts / "bun-runner.js"), str(scripts / "worker-service.cjs"), "hook", "codex",
                           "context"], input=json.dumps(event), capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=120)
    out = proc.stdout.strip()
    try:
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return out  # Codex also accepts plain stdout as context

def newest_rollout(home: Path, after: float):
    logs = [p for p in (home / "sessions").rglob("rollout-*.jsonl") if p.stat().st_mtime >= after - 5]
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def score(result_text: str, questions: dict) -> dict:
    m = re.search(r"ANSWERS:\s*(\{.*\})", result_text or "", re.S)
    answers = {}
    if m:
        try:
            answers = json.loads(m.group(1))
        except json.JSONDecodeError:
            answers = {}
    out = {"answers": answers, "correct": {}, "by_kind": {}}
    for q, (_, accepted, kind) in questions.items():
        a = str(answers.get(q) or "").lower()
        ok = any(x.lower() in a for x in accepted)
        out["correct"][q] = ok
        k = out["by_kind"].setdefault(kind, [0, 0])
        k[0] += ok
        k[1] += 1
    out["score"] = sum(out["correct"].values())
    out["of"] = len(questions)
    return out


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("arm", choices=sorted(set(ARM_PROMPT) | set(CROSS)))
    parser.add_argument("tasks", nargs="*", help="billing, retry, export (default: all)")
    parser.add_argument("--copy-codex-auth", action="store_true")
    args = parser.parse_args()
    arm, tasks = args.arm, args.tasks or list(TASKS)
    unknown = set(tasks) - set(TASKS)
    if unknown:
        parser.error(f"unknown task(s): {', '.join(sorted(unknown))}")
    if not CLAUDE:
        raise SystemExit("Claude Code not found: set CLAUDE_BIN")
    if arm in CROSS and not CODEX:
        raise SystemExit("Codex not found: set CODEX_BIN")
    home = prepare_codex_home(arm, args.copy_codex_auth) if arm in CROSS else None
    try:
        _run_tasks(arm, tasks)
    finally:
        if home is not None:
            (home / "auth.json").unlink(missing_ok=True)  # never leave a credential copy behind


def _run_tasks(arm, tasks):
    for name in tasks:
        task = TASKS[name]
        # Cross-agent repos live outside the home folder: under it, Codex picks up
        # ~/.codex/config.toml as project config (the user's plugins and MCP servers).
        repo = (CROSS_REPOS if arm in CROSS else HERE / "repos") / arm / f"{name}-app"
        if repo.exists():
            shutil.rmtree(repo)
        for rel, text in task["files"].items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text, encoding="utf-8")
        first = CROSS.get(arm, arm)  # the system's Claude Code setup for session 1
        s1 = run_claude(first, repo, task["session1"])
        if first == "claudemem":
            s1["claudemem_wait_s"] = wait_for_claudemem(s1.get("session_id"))
        quarantine_transcripts(s1.get("session_id"), f"{arm}-{name}-s1")
        qs = {q: text for q, (text, _, _) in task["questions"].items()}
        prompt2 = SESSION2.format(questions=json.dumps(qs))
        if arm == "x_cognicore_hooksim":
            brief = run_hook("session-start", {"cwd": str(repo), "source": "startup", "hook_event_name": "SessionStart"})
            context = json.loads(brief)["hookSpecificOutput"]["additionalContext"] if brief.strip() else ""
            started = time.time()
            s2 = run_codex(arm, repo, prompt2, developer_context=context)
            s2["hook_brief_tokens"] = len(context) // 4
            rollout = newest_rollout(codex_home(arm), after=started)
            s2["rollout"] = str(rollout) if rollout else None
            if rollout:
                run_hook("save", {"session_id": rollout.stem, "transcript_path": str(rollout), "cwd": str(repo),
                                  "hook_event_name": "Stop"}, agent="codex")
        elif arm == "x_claudemem_hooksim":
            # claude-mem's own Codex SessionStart command, run as Codex would run it.
            context = claudemem_codex_context({"session_id": f"sim-{name}", "transcript_path": None, "cwd": str(repo),
                                               "hook_event_name": "SessionStart", "source": "startup", "model": "codex"})
            s2 = run_codex(arm, repo, prompt2, developer_context=context)
            s2["hook_brief_tokens"] = len(context) // 4
        elif arm in CROSS:
            s2 = run_codex(arm, repo, prompt2)
        else:
            s2 = run_claude(arm, repo, prompt2)
        check = subprocess.run([sys.executable, "-c", task["done_check"]], cwd=repo, capture_output=True, text=True)
        record = {"arm": arm, "task": name, "session1": s1, "session2": s2,
                  "quiz": score(s2.get("result", ""), task["questions"]),
                  "followup_done": check.returncode == 0, "followup_error": check.stderr.strip()[-300:]}
        out = HERE / "results" / arm / f"{name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
        print(arm, name, "quiz", record["quiz"]["score"], "/", record["quiz"]["of"], "followup", record["followup_done"],
              "s1 in/out", s1.get("total_input"), s1.get("output_tokens"), "s2 in/out", s2.get("total_input"),
              s2.get("output_tokens"), flush=True)


if __name__ == "__main__":
    main()

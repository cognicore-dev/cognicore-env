"""Benchmark cross-agent memory transfer: save_session -> resume_session.

Seeds sample sessions with known facts, saves them, resumes, and reports
tokens, latency and a "facts recovered" score: the percentage of seeded
values that appear verbatim in the resume output (case-insensitive). The
values are exact strings an agent needs to continue the work (ids,
versions, paths, numbers), so verbatim presence is the bar.

Usage:
    python scripts/bench_transfer.py                      # this checkout
    python scripts/bench_transfer.py --repo ../other-tree # e.g. a baseline worktree
    python scripts/bench_transfer.py --out results.json

Runs fully offline with BM25 search; pass --semantic to use the
sentence-transformers provider if it is installed. Token counts use the
repo's estimator (chars / 4).
"""

import argparse
import inspect
import json
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

FAKE_SECRET = "sk-test-FAKE0000000000000000000000"

SESSIONS = {
    "s1_jwt_debug": {
        "query": "JWT 401 auth bug",
        "conversation": [
            {"role": "user", "content": "Hey! Can you help me with something?"},
            {"role": "assistant", "content": "Sure, what are you working on?"},
            {"role": "user", "content": "Our goal is to fix intermittent 401s on /api/v2/orders. Ticket is AUTH-1423. We run Python 3.11.4 and FastAPI 0.110.0."},
            {"role": "assistant", "content": "Let me check. I looked at auth/tokens.py. The root cause is clock skew between pods; tokens are rejected a few seconds early."},
            {"role": "user", "content": "We tried increasing the token TTL to 24h but it did not help."},
            {"role": "assistant", "content": "Right, TTL does not affect the nbf check. We decided to switch from python-jose to PyJWT 2.8.0 because python-jose is unmaintained, and set leeway=30 seconds in decode()."},
            {"role": "user", "content": "Great, thanks! Committed as 9f3c2ab."},
            {"role": "assistant", "content": "Nice. TODO: add a regression test for expired tokens in tests/test_auth.py."},
            {"role": "user", "content": "Sounds good."},
        ],
        "facts": {
            "goal": "/api/v2/orders", "ticket": "AUTH-1423", "python": "3.11.4",
            "framework": "0.110.0", "file": "auth/tokens.py", "root_cause": "clock skew",
            "rejected": "24h", "decision": "PyJWT 2.8.0", "value": "leeway=30",
            "commit": "9f3c2ab", "open_task": "tests/test_auth.py",
        },
        "environment": {"os": "macOS", "python_version": "3.11.4", "framework": "FastAPI",
                        "framework_version": "0.110.0", "dependencies": {"pyjwt": "2.8.0"}},
    },
    "s2_launch_plan": {
        "query": "product launch plan",
        "conversation": [
            {"role": "user", "content": "Let's plan the launch."},
            {"role": "assistant", "content": "Okay! What do we know so far?"},
            {"role": "user", "content": "Our product launch is on November 15. Target audience is indie developers. Brand color is #111111."},
            {"role": "assistant", "content": "Got it. What about pricing?"},
            {"role": "user", "content": "Pricing is $12/month for the Pro plan. The domain is cognicore.dev and the waitlist has 1,240 signups."},
            {"role": "assistant", "content": "We agreed to launch on Product Hunt first, then Hacker News a week later, because PH drives indie devs."},
            {"role": "user", "content": "Yes. Next step: Priya needs to finish the landing page copy by Nov 8."},
            {"role": "assistant", "content": "Noted. Anything else?"},
            {"role": "user", "content": "No, that's all for now, thanks!"},
        ],
        "facts": {
            "date": "November 15", "audience": "indie developers", "color": "#111111",
            "price": "$12", "domain": "cognicore.dev", "waitlist": "1,240",
            "decision": "Product Hunt", "owner": "Priya", "deadline": "Nov 8",
        },
    },
    "s3_db_migration": {
        "query": "Postgres migration",
        "conversation": [
            {"role": "user", "content": "We are migrating the users table from MySQL 5.7 to Postgres 16. It has 3.2 million rows."},
            {"role": "assistant", "content": "Okay. Which migration file?"},
            {"role": "user", "content": "migrations/0042_rename_user_id.sql renames user_id to account_id. The new env var is DATABASE_URL_V2."},
            {"role": "assistant", "content": "pgloader failed with error code 42P01 on the sessions table because it was created after the snapshot."},
            {"role": "user", "content": f"Here is the staging key in case you need it: {FAKE_SECRET}"},
            {"role": "assistant", "content": "We chose dual-write for two weeks instead of a big-bang cutover, so rollback stays cheap."},
            {"role": "user", "content": "Pending: backfill account_id for the 18,000 orphaned rows. The workers run Node 20.11.1."},
            {"role": "assistant", "content": "```sql\nUPDATE accounts SET account_id = user_id WHERE account_id IS NULL;\n```"},
            {"role": "user", "content": "Perfect, cheers."},
        ],
        "facts": {
            "goal": "Postgres 16", "rows": "3.2 million", "file": "0042_rename_user_id.sql",
            "env_var": "DATABASE_URL_V2", "error": "42P01", "decision": "dual-write",
            "open_task": "18,000", "runtime": "20.11.1", "code": "SET account_id = user_id",
        },
    },
}

# Held out: written after the extraction rules were tuned on SESSIONS, and not
# used for tuning. Terser, chat-style phrasing on purpose.
HELDOUT = {
    "h1_frontend_bug": {
        "query": "checkout double submit",
        "conversation": [
            {"role": "user", "content": "the checkout button double-submits on slow networks, repro on Safari 17.4. it's in src/components/CheckoutButton.tsx"},
            {"role": "assistant", "content": "I see. The onClick handler isn't disabled while the request is pending. React 18.2.0 batches state, so setLoading(true) lands after the second click."},
            {"role": "user", "content": "ok so what do we do"},
            {"role": "assistant", "content": "Plan: wrap the submit in a useRef guard and disable the button until the POST /api/checkout resolves. I'd avoid debounce because it delays legit clicks."},
            {"role": "user", "content": "go with the ref guard. PR is #882"},
            {"role": "assistant", "content": "Done, pushed to branch fix/double-submit. Still open: add a Playwright test in e2e/checkout.spec.ts and ask Dana to QA on iOS."},
            {"role": "user", "content": "👍"},
        ],
        "facts": {
            "browser": "Safari 17.4", "file": "src/components/CheckoutButton.tsx", "react": "18.2.0",
            "endpoint": "/api/checkout", "decision": "ref guard", "pr": "#882", "branch": "fix/double-submit",
            "test": "e2e/checkout.spec.ts", "owner": "Dana", "rejected": "debounce",
        },
    },
    "h2_training_run": {
        "query": "training run diverged",
        "conversation": [
            {"role": "user", "content": "Training run r-0917 diverged at step 12k, loss went NaN."},
            {"role": "assistant", "content": "Looks like the LR warmup is too short. You're at lr 3e-4 with 500 warmup steps on a batch of 2048."},
            {"role": "user", "content": "makes sense. we'll bump warmup to 2000 steps and turn on grad clipping at 1.0"},
            {"role": "assistant", "content": "Good call. Also the checkpoint at s3://ml-ckpt/r-0917/step-11000 is the last clean one, resume from there."},
            {"role": "user", "content": "cool. remind me tomorrow to compare against baseline run r-0899, eval F1 was 0.871"},
            {"role": "assistant", "content": "Will do."},
        ],
        "facts": {
            "run": "r-0917", "step": "12k", "lr": "3e-4", "warmup": "2000", "clip": "1.0",
            "checkpoint": "s3://ml-ckpt/r-0917/step-11000", "baseline": "r-0899", "f1": "0.871", "batch": "2048",
        },
    },
    "h3_prod_incident": {
        "query": "payments-api outage",
        "conversation": [
            {"role": "user", "content": "prod is throwing 503s since 14:05 UTC, the payments-api pods are in CrashLoopBackOff"},
            {"role": "assistant", "content": "kubectl logs shows OOMKilled; memory limit is 512Mi and the new build v2.14.0 loads the full fraud model at startup."},
            {"role": "user", "content": "rollback?"},
            {"role": "assistant", "content": "Rolling back to v2.13.2 now with helm rollback payments-api 41."},
            {"role": "user", "content": "ok it's recovering. long term?"},
            {"role": "assistant", "content": "Raise the limit to 1Gi and lazy-load the model. I'll open INC-2291 for the postmortem."},
            {"role": "user", "content": "also tell Marco the on-call handover is at 18:00"},
        ],
        "facts": {
            "status": "503", "time": "14:05", "service": "payments-api", "cause": "OOMKilled",
            "limit": "512Mi", "bad_version": "v2.14.0", "good_version": "v2.13.2", "revision": "payments-api 41",
            "new_limit": "1Gi", "incident": "INC-2291", "owner": "Marco", "handover": "18:00",
        },
    },
}

# What a careful agent would send as handoff_json instead of the conversation.
# Written with the seeded facts in view, so "facts recovered" on this path
# mostly shows that supplied values survive verbatim; the point is token cost.
AGENT_HANDOFFS = {
    "s1_jwt_debug": {
        "goal": "Fix intermittent 401s on /api/v2/orders (AUTH-1423)",
        "current_state": "Root cause: clock skew between pods. Fix committed as 9f3c2ab",
        "decisions": [{"what": "Switch python-jose -> PyJWT 2.8.0, leeway=30", "why": "python-jose unmaintained"},
                      "Rejected: token TTL 24h (no effect on nbf)"],
        "open_tasks": ["Regression test for expired tokens in tests/test_auth.py"],
        "key_facts": ["Python 3.11.4, FastAPI 0.110.0"],
        "files": ["auth/tokens.py"],
    },
    "s2_launch_plan": {
        "goal": "Plan product launch on November 15 for indie developers",
        "decisions": [{"what": "Launch on Product Hunt first, Hacker News a week later", "why": "PH drives indie devs"}],
        "open_tasks": ["Priya: landing page copy by Nov 8"],
        "key_facts": ["Brand color #111111", "Pro plan $12/month", "Domain cognicore.dev", "Waitlist 1,240 signups"],
    },
    "s3_db_migration": {
        "goal": "Migrate users table (3.2 million rows) from MySQL 5.7 to Postgres 16",
        "current_state": "pgloader failed with 42P01 on the sessions table (created after the snapshot)",
        "decisions": [{"what": "Dual-write for two weeks instead of big-bang cutover", "why": "cheap rollback"}],
        "open_tasks": ["Backfill account_id for 18,000 orphaned rows"],
        "key_facts": ["migrations/0042_rename_user_id.sql renames user_id -> account_id", "New env var DATABASE_URL_V2",
                      "Workers run Node 20.11.1",
                      "Backfill SQL: UPDATE accounts SET account_id = user_id WHERE account_id IS NULL"],
    },
    "h1_frontend_bug": {
        "goal": "Fix checkout button double-submit on slow networks (Safari 17.4)",
        "current_state": "useRef guard pushed to branch fix/double-submit, PR #882",
        "decisions": [{"what": "ref guard + disable button until POST /api/checkout resolves",
                       "why": "debounce delays legit clicks"}, "Rejected: debounce"],
        "open_tasks": ["Playwright test in e2e/checkout.spec.ts", "Dana to QA on iOS"],
        "key_facts": ["React 18.2.0 batches state, so setLoading(true) lands after the second click"],
        "files": ["src/components/CheckoutButton.tsx"],
    },
    "h2_training_run": {
        "goal": "Recover training run r-0917 (diverged at step 12k, loss NaN)",
        "current_state": "Resume from s3://ml-ckpt/r-0917/step-11000 (last clean checkpoint)",
        "decisions": [{"what": "Warmup 500 -> 2000 steps and grad clipping at 1.0",
                       "why": "warmup too short for lr 3e-4 at batch 2048"}],
        "open_tasks": ["Tomorrow: compare against baseline run r-0899 (eval F1 0.871)"],
    },
    "h3_prod_incident": {
        "goal": "Resolve payments-api outage: 503s since 14:05 UTC, pods in CrashLoopBackOff",
        "current_state": "Rolled back v2.14.0 -> v2.13.2 (helm rollback payments-api 41); recovering",
        "decisions": [{"what": "Long term: memory limit 512Mi -> 1Gi and lazy-load the fraud model",
                       "why": "OOMKilled at startup"}],
        "open_tasks": ["Open INC-2291 postmortem", "Tell Marco: on-call handover at 18:00"],
    },
}

CHATTER = re.compile(r"^(hey|hi|ok|okay|sure|thanks|great|nice|perfect|sounds good|got it|noted|yes|no)\b", re.I)
FILLER = [
    {"role": "user", "content": "Ok, let me think about that for a second and get back to you shortly."},
    {"role": "assistant", "content": "Sure, take your time. I will wait here until you are ready to continue."},
]


def recovered(text, facts):
    low = text.lower()
    hit = [k for k, v in facts.items() if v.lower() in low]
    return round(100 * len(hit) / len(facts), 1), sorted(set(facts) - set(hit))


def median_ms(fn, n=5):
    out, times = None, []
    for _ in range(n):
        t = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t) * 1000)
    return out, round(statistics.median(times), 1)


def run(repo: Path, semantic: bool) -> dict:
    sys.path.insert(0, str(repo))
    from cognicore.memory import SQLiteMemoryBackend
    from cognicore.memory.context_preservation import TokenBudget, compress_context, resume_session, save_session

    resume_params = set(inspect.signature(resume_session).parameters)
    save_params = set(inspect.signature(save_session).parameters)
    has_budget = "max_tokens" in resume_params
    has_env = "environment" in save_params

    provider = None
    if semantic:
        from cognicore.memory.providers.sentence_transformers import SentenceTransformerProvider
        provider = SentenceTransformerProvider()

    work = Path(tempfile.mkdtemp(prefix="bench_transfer_"))
    counter = [0]

    def fresh():
        counter[0] += 1
        return SQLiteMemoryBackend(str(work / f"db{counter[0]}.db"), provider=provider)

    def save(b, s, name, agent=""):
        kwargs = {"session_name": name}
        if "agent" in save_params:
            kwargs["agent"] = agent
        if has_env and s.get("environment"):
            kwargs["environment"] = s["environment"]
        return save_session(b, s["conversation"], **kwargs)

    def rows_of(b):
        with b._get_conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM memory_entries").fetchall()]

    def row_stats(rows):
        texts = [r["text"] for r in rows]
        norm = [re.sub(r"[^a-z0-9]", "", t.lower()) for t in texts]
        return {
            "rows_stored": len(rows),
            "stored_tokens": sum(len(t) for t in texts) // 4,
            "duplicate_rows": len(norm) - len(set(norm)),
            "chatter_rows": sum(bool(CHATTER.match(t.strip())) for t in texts),
            "rows_with_source_agent": sum(bool(r.get("source_agent")) for r in rows),
        }

    has_handoff = "supplied_handoff" in save_params

    def est(obj):
        return TokenBudget.estimate_tokens(obj if isinstance(obj, str) else json.dumps(obj))

    def agent_path(handoff, name, facts):
        """Save from an agent-written handoff (no conversation), then resume."""
        b = fresh()
        reply = save_session(b, supplied_handoff=handoff, session_name=name, agent="claude-code")
        out = resume_session(b)
        return {"save_args_tokens": est({"handoff_json": json.dumps(handoff), "session_name": name}),
                "save_response_tokens": est(reply), "resume_tokens": est(out),
                "facts_recovered_pct": recovered(out, facts)[0]}

    report = {"repo": str(repo), "semantic": semantic,
              "api": {"max_tokens": has_budget, "environment": has_env, "handoff_json": has_handoff},
              "tool_definition_tokens": tool_definition_tokens(), "sessions": {}}

    all_sessions = [("dev", n, s) for n, s in SESSIONS.items()] + [("heldout", n, s) for n, s in HELDOUT.items()]
    for group, name, s in all_sessions:
        facts = s["facts"]
        save_times = []
        for _ in range(5):
            b = fresh()
            t = time.perf_counter()
            save(b, s, name, agent="claude-code")
            save_times.append((time.perf_counter() - t) * 1000)
        b = fresh()
        save(b, s, name, agent="claude-code")
        rows = rows_of(b)
        brief, resume_ms = median_ms(lambda: resume_session(b))
        brief_q = resume_session(b, query=s["query"])
        entry = {
            "group": group,
            "input_tokens": TokenBudget.estimate_tokens(s["conversation"]),
            "save_ms_median": round(statistics.median(save_times), 1),
            **row_stats(rows),
            "resume_tokens": TokenBudget.estimate_tokens(brief),
            "resume_ms_median": resume_ms,
            "facts_recovered_pct": recovered(brief, facts)[0],
            "missed": recovered(brief, facts)[1],
            "facts_recovered_with_query_pct": recovered(brief_q, facts)[0],
            "secret_stored": any(FAKE_SECRET in r["text"] for r in rows),
            "secret_in_resume": FAKE_SECRET in brief,
        }
        # Token cost of the transfer itself: what the agent sends, what it reads back.
        entry["save_args_tokens"] = est({"conversation": s["conversation"], "session_name": name})
        entry["save_response_tokens"] = est(save(fresh(), s, name, agent="claude-code"))
        n_recovered = round(entry["facts_recovered_pct"] * len(facts) / 100)
        entry["resume_tokens_per_fact"] = round(entry["resume_tokens"] / max(1, n_recovered), 1)
        if has_handoff and name in AGENT_HANDOFFS:
            entry["agent_handoff"] = agent_path(AGENT_HANDOFFS[name], name, facts)
        if has_budget:
            for budget in (150, 300):
                small = resume_session(b, max_tokens=budget)
                entry[f"budget_{budget}_tokens"] = TokenBudget.estimate_tokens(small)
                entry[f"budget_{budget}_facts_recovered_pct"] = recovered(small, facts)[0]
        comp = json.loads(compress_context(fresh(), s["conversation"], keep_last_n=5))
        kept = comp["summary"] + "\n" + "\n".join(m["content"] for m in s["conversation"][-5:])
        entry["compress"] = {"ratio": comp["compression_ratio"], "tokens_after": comp["tokens_after"],
                             "facts_kept_pct": recovered(kept, facts)[0],
                             "secret_kept": FAKE_SECRET in comp["summary"]}
        comp0 = json.loads(compress_context(fresh(), s["conversation"], keep_last_n=0))
        entry["compress"]["keep_last_n_0_messages_compressed"] = comp0["messages_compressed"]
        report["sessions"][name] = entry

    # Long session: all topics, filler chatter, restated lines (105 messages).
    long_conv, long_facts = [], {}
    for name, s in SESSIONS.items():
        long_conv += s["conversation"] + FILLER * 12 + s["conversation"][2:4]
        long_facts.update({f"{name}.{k}": v for k, v in s["facts"].items()})
    b = fresh()
    t = time.perf_counter()
    long_reply = save_session(b, long_conv, session_name="long")
    long_save_ms = round((time.perf_counter() - t) * 1000, 1)
    brief = resume_session(b)
    report["long_session"] = {
        "messages": len(long_conv), "input_tokens": TokenBudget.estimate_tokens(long_conv),
        "save_ms": long_save_ms, **row_stats(rows_of(b)),
        "save_args_tokens": est({"conversation": long_conv, "session_name": "long"}),
        "save_response_tokens": est(long_reply),
        "resume_tokens": TokenBudget.estimate_tokens(brief),
        "facts_recovered_pct": recovered(brief, long_facts)[0],
    }
    if has_handoff:
        parts = [AGENT_HANDOFFS[n] for n in SESSIONS]
        merged = {"goal": "; ".join(p["goal"] for p in parts),
                  "current_state": "; ".join(p["current_state"] for p in parts if p.get("current_state"))}
        for key in ("decisions", "open_tasks", "key_facts", "files"):
            merged[key] = [x for p in parts for x in p.get(key, [])]
        report["long_session"]["agent_handoff"] = agent_path(merged, "long", long_facts)
    if has_budget:
        small = resume_session(b, max_tokens=500)
        report["long_session"]["budget_500_tokens"] = TokenBudget.estimate_tokens(small)
        report["long_session"]["budget_500_facts_recovered_pct"] = recovered(small, long_facts)[0]

    # Cross-agent handoff: three agents each save one session into the same memory
    # (1.1 s apart so the baseline's per-second session ids do not collide), then a
    # new agent resumes each topic by query, and the latest one with no query.
    def cross_agent(sessions):
        b = fresh()
        for (name, s), agent in zip(sessions.items(), ["claude", "chatgpt", "cursor"]):
            save(b, s, name, agent=agent)
            time.sleep(1.1)
        cross = {}
        for name, s in sessions.items():
            out, ms = median_ms(lambda: resume_session(b, query=s["query"]), n=3)
            cross[name] = {"facts_recovered_with_query_pct": recovered(out, s["facts"])[0],
                           "tokens": TokenBudget.estimate_tokens(out), "resume_ms": ms}
        latest = resume_session(b)
        first, last = list(sessions.values())[0], list(sessions.values())[-1]
        cross["latest_no_query_recovered_pct"] = recovered(latest, last["facts"])[0]
        cross["oldest_no_query_recovered_pct"] = recovered(latest, first["facts"])[0]
        cross["no_query_tokens"] = TokenBudget.estimate_tokens(latest)
        return cross

    report["cross_session"] = {"dev": cross_agent(SESSIONS), "heldout": cross_agent(HELDOUT)}

    # Same-second saves and re-saving the same conversation.
    b = fresh()
    save_session(b, SESSIONS["s1_jwt_debug"]["conversation"], session_name="A")
    save_session(b, SESSIONS["s2_launch_plan"]["conversation"], session_name="B")
    with b._get_conn() as conn:
        report["same_second_saves_session_rows"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    b = fresh()
    save_session(b, SESSIONS["s2_launch_plan"]["conversation"])
    n1 = len(rows_of(b))
    time.sleep(1.1)
    save_session(b, SESSIONS["s2_launch_plan"]["conversation"])
    report["resave_rows"] = [n1, len(rows_of(b))]

    if has_env:
        b = fresh()
        save(b, SESSIONS["s1_jwt_debug"], "s1")
        out = resume_session(b, environment={"os": "Windows", "python_version": "3.12.1",
                                             "framework": "FastAPI", "framework_version": "0.115.0",
                                             "dependencies": {"pyjwt": "1.7.1"}})
        report["environment_warning_shown"] = "Environment warnings" in out
    return report


def tool_definition_tokens() -> dict:
    """Tokens every client loads into context on every turn: all tool names, descriptions and schemas."""
    import asyncio

    def total(tools):
        return sum(len(json.dumps({"name": t.name, "description": t.description, "input_schema": t.inputSchema}))
                   for t in tools) // 4

    out = {}
    try:
        from cognicore.mcp import server as local
        out["local"] = total(asyncio.run(local.create_mcp_server().list_tools()))
    except Exception as e:  # mcp not installed
        out["local"] = f"unavailable: {type(e).__name__}"
    try:
        from cognicore.connectors import remote
        out["remote"] = total(asyncio.run(remote.mcp.list_tools()))
    except Exception as e:  # server extras not installed
        out["remote"] = f"unavailable: {type(e).__name__}"
    return out


def summarize(r: dict) -> str:
    lines = ["| Session | Save ms | Rows | Dupes | Chatter | Resume tok | Facts (no query / query) |",
             "|---|---|---|---|---|---|---|"]
    for name, s in r["sessions"].items():
        lines.append(f"| {name} ({s['group']}) | {s['save_ms_median']} | {s['rows_stored']} | {s['duplicate_rows']} | "
                     f"{s['chatter_rows']} | {s['resume_tokens']} | {s['facts_recovered_pct']}% / "
                     f"{s['facts_recovered_with_query_pct']}% |")
    ls = r["long_session"]
    lines.append(f"| long ({ls['messages']} msgs) | {ls['save_ms']} | {ls['rows_stored']} | {ls['duplicate_rows']} | "
                 f"{ls['chatter_rows']} | {ls['resume_tokens']} | {ls['facts_recovered_pct']}% |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="checkout to import cognicore from (default: this one)")
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--semantic", action="store_true", help="use sentence-transformers embeddings")
    args = parser.parse_args()

    report = run(args.repo.resolve(), args.semantic)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2))
    print(summarize(report))
    print(json.dumps({k: v for k, v in report.items() if k not in ("sessions", "long_session")}, indent=2))


if __name__ == "__main__":
    main()

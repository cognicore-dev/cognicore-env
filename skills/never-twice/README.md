# Never Twice

**Your agent never repeats a mistake.** Correct it once ("no, we use pnpm here, npm breaks our lockfile") and next time it checks that lesson before acting, and tells you it did.

Other self-improving skills write lessons down. Never Twice makes sure they're used: before a task, the agent checks only the few lessons that match it.

## What this skill can access

| | |
|---|---|
| **Reads** | the one-line task description the agent passes to `cognicore lesson check` |
| **Writes** | `~/.cognicore/mcp/lessons.db` only |
| **Network** | none |
| **Shell commands** | only `cognicore lesson …` |
| **Secrets** | none needed; lessons are passed through secret redaction before they're stored |

## Install

```bash
pipx install "cognicore-env>=0.13"       # or: uv tool install "cognicore-env>=0.13"
openclaw skills install @cognicore-dev/never-twice
```

The `cognicore` command is open source (Apache 2.0). The lesson store is one file: [`cognicore/lessons.py`](https://github.com/cognicore-dev/cognicore-env/blob/main/cognicore/lessons.py).

## How it works

- **Checked before acting.** `cognicore lesson check "<task>"` prints at most 3 relevant lessons, about 200 tokens at most, and nothing at all when none match, so it costs nothing on unrelated work.
- **Remembers why.** Each lesson keeps the mistake, the fix and the reason.
- **No pile-up.** Recording the same lesson again merges it and counts it ("seen 3x").
- **Ranked search.** Lessons are matched with SQLite full-text search (BM25). A lesson's `--when` words ("installing packages") let it match tasks worded differently ("add axios").
- **Per project or global.** Lessons apply to the folder they were learned in, unless recorded with `--scope global`.

## Review or delete

```bash
cognicore lesson list            # this project's and global lessons
cognicore lesson list --all      # every project
cognicore lesson forget 12       # delete lesson #12
```

To wipe everything, delete `~/.cognicore/mcp/lessons.db`.

## Uninstall

Remove the skill in OpenClaw, then:

```bash
pipx uninstall cognicore-env     # removes the cognicore command; lessons.db stays until you delete it
```

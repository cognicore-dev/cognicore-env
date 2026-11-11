---
name: never-twice
description: Self-improving agent that never repeats a mistake. Learns from every correction and failure, remembers why, and checks only the relevant lessons before acting. Local-first, open source.
version: 1.0.0
metadata:
  openclaw:
    requires:
      bins:
        - cognicore
    homepage: https://github.com/cognicore-dev/cognicore-env/tree/main/skills/never-twice
---

# Never Twice

You learn from mistakes and never repeat them. Lessons are stored by the `cognicore` command and checked before you act.

## Before any non-trivial task

Run:

    cognicore lesson check "<one line: the kind of action, then the specifics>"

Describe the kind of action in general words as well as the specifics, for example
`"install a package: add axios"` or `"format dates in the invoice export"`. Lessons are matched by words.

If it prints lessons, follow them, and tell the user in one short line which lesson you applied, for example:
"Using pnpm (lesson: npm breaks this repo's lockfile)." If it prints nothing, carry on; there is nothing to apply.
If a lesson clearly doesn't apply to this task, ignore it and don't mention it.

## Record a lesson when

- the user corrects you ("no", "that's wrong", "actually…", "don't do that")
- a command or tool fails and you find the fix
- the user undoes or rewrites something you did
- you discover a project rule the hard way

Run:

    cognicore lesson add --mistake "<what went wrong>" --fix "<what works>" --why "<the reason>" --when "<the situation, in general words>"

- Always include `--why`. One lesson per mistake, each field one sentence.
- `--when` names the situation broadly ("installing or adding packages", "writing dates"), so the lesson is found next time even when the task is worded differently.
- Add `--scope global` only for lessons that apply in every project; the default is this project.
- Recording the same lesson again is fine: it is merged and counted, not duplicated.

## Never

- store passwords, tokens, keys or personal data in a lesson
- record a lesson the user asked you not to keep

## Privacy

Lessons are stored locally in the CogniCore data folder (`~/.cognicore/mcp/lessons.db`). Nothing leaves this machine.
Users can review lessons with `cognicore lesson list` and delete one with `cognicore lesson forget <id>`.

---
name: recall
description: Search CogniCore memory for stored facts, decisions, or context relevant to a query.
argument-hint: "<query> [--top <n>] [--scope user|project] [--category <category>]"
---

Search CogniCore persistent memory using `cognicore_recall` with the given query.

Parse the input as follows:
- The main query text is everything before any `--` flags.
- `--top` sets the number of results to return (default: 5).
- `--scope` filters by scope: "user" or "project" (default: "user").
- `--category` filters by category.

Display results as a numbered list with the memory ID and text. If nothing is found, say so clearly and suggest using `/memory-list` to browse all stored memories.

## Examples

```
/recall what database does this project use
/recall my code style preferences --scope user
/recall bug fixes --category debugging --top 10
```

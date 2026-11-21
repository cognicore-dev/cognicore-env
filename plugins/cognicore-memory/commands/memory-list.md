---
name: memory-list
description: List recently stored CogniCore memories.
argument-hint: "[--limit <n>] [--scope user|project] [--category <category>]"
---

List stored memories using `cognicore_list`.

Parse the input as follows:
- `--limit` sets the number of memories to show (default: 10).
- `--scope` filters by scope: "user" or "project".
- `--category` filters by category.

Display results as a numbered list showing each memory's ID, category, and text. Group by scope if both scopes are present.

## Examples

```
/memory-list
/memory-list --limit 20
/memory-list --scope project --category architecture
```

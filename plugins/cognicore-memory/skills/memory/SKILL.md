---
name: memory
description: Guidelines for when to recall stored context and when to offer to save facts, preferences, project decisions and previous solutions with the CogniCore memory tools.
---

# CogniCore Memory Guidelines

You have access to a persistent memory backend via the `cognicore` MCP server. This allows you to remember important information across conversations for the user.

## When to Remember
- Offer to remember explicit user preferences (e.g., "always use TypeScript", "prefer dark mode").
- Store project-wide architectural constraints and technical decisions when discussing a project.
- Store solutions to tricky bugs or environmental setup instructions so you do not have to solve them again in the future.
- When the user explicitly asks you to remember a fact or context.
- Except when the user explicitly asked, confirm with the user before calling `cognicore_remember`.

## When to Recall
- Actively use `cognicore_recall` at the start of a new project session to check if there are known constraints or context.
- If a user asks "what was the database we chose?", use `cognicore_recall` before answering.
- If you face a cryptic bug, check if a solution was previously stored.

## Project vs User Scope
- Use **project** scope when the information is specific to the current codebase or project context. (e.g., "This project uses FastAPI.")
- Use **user** scope when the information is a global preference or personal fact about the user. (e.g., "I prefer verbose explanations.")

## What NOT to store
- Do NOT store temporary debugging context or one-off scratchpad logic.
- Do NOT store secrets, API keys, or raw passwords.

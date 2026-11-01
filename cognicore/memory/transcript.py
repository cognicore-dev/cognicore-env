"""
Read a Claude Code session transcript (JSONL) into a conversation for save_session.

Used by the automatic-capture hooks, so nothing depends on the agent
remembering to save. Only what was *said* is kept:

- user prompts and assistant text, with injected context removed
  (``<system-reminder>`` blocks, hook output, slash-command wrappers,
  and CogniCore's own resume brief, so it is not saved back as new facts);
- the paths of files the agent edited or created;
- any ``handoff_json`` the agent passed to ``cognicore_save_session``, merged
  in by save_session.

Tool results (file contents, command output) and the raw request blobs Claude
Code also writes, which repeat system context such as the account email, are
never read.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_SAVE_TOOL = re.compile(r"(?:^|__)cognicore_save_session$")

# Injected text, not something the user or the agent said.
_INJECTED = [
    re.compile(r"<system-reminder>.*?</system-reminder>", re.S),
    re.compile(r"<(command-[a-z]+|local-command-[a-z]+)>.*?</\1>", re.S),
    re.compile(r"=== COGNICORE CONTEXT RESUME ===.*?=== END CONTEXT[^\n]*", re.S),
]
_SKIP_PREFIXES = ("[Request interrupted", "Caveat: The messages below", "SessionStart hook additional context")


def _clean(text: str) -> str:
    for pattern in _INJECTED:
        text = pattern.sub(" ", text)
    text = text.strip()
    return "" if text.startswith(_SKIP_PREFIXES) else text


def _relative(path: str, cwd: str) -> str:
    try:
        rel = os.path.relpath(path, cwd)
    except ValueError:  # different drive on Windows
        return path
    return path if rel.startswith("..") else rel.replace("\\", "/")


def _entries(path: str) -> List[Dict[str, Any]]:
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def read_transcript(path: str, cwd: str = "") -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """Return ``(conversation, supplied_handoff)`` from a Claude Code or Codex transcript file.

    ``supplied_handoff`` merges every handoff the agent saved through the
    CogniCore tool during the session, plus the files it edited.
    """
    entries = _entries(path)
    if any(e.get("type") in ("session_meta", "response_item") and "payload" in e for e in entries[:20]):
        return _read_codex(entries, cwd)
    return _read_claude(entries, cwd)


def _read_claude(entries: List[Dict[str, Any]], cwd: str) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    conversation: List[Dict[str, str]] = []
    supplied: Dict[str, Any] = {}
    files: List[str] = []

    for entry in entries:
        kind = entry.get("type")
        if kind not in ("user", "assistant") or entry.get("isMeta") or entry.get("isSidechain"):
            continue
        content = (entry.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        texts = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                cleaned = _clean(block.get("text", ""))
                if cleaned:
                    texts.append(cleaned)
            elif block.get("type") == "tool_use" and kind == "assistant":
                name, args = block.get("name", ""), block.get("input") or {}
                if name in _EDIT_TOOLS and isinstance(args, dict):
                    target = args.get("file_path") or args.get("notebook_path")
                    if target:
                        rel = _relative(str(target), cwd) if cwd else str(target)
                        if rel not in files:
                            files.append(rel)
                elif _SAVE_TOOL.search(name) and isinstance(args, dict):
                    _merge_supplied(supplied, args.get("handoff_json"))
            # tool_result, thinking and image blocks are deliberately ignored
        if texts:
            conversation.append({"role": kind, "content": "\n".join(texts)})

    return conversation, _with_files(supplied, files)


# Codex session logs ("rollouts"): one {"type", "payload"} object per line.
_CODEX_INJECTED = [
    re.compile(r"<(environment_context|user_instructions|INSTRUCTIONS|skills_instructions)>.*?</\1>", re.S),
    re.compile(r"^#\s*AGENTS\.md instructions\b[^\n]*", re.M),  # heading of Codex's AGENTS.md injection
]
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.M)
_SHELL_WRITE = re.compile(r"(?:Set-Content|Add-Content|Out-File|New-Item)\b[^\n]*?-(?:Path|FilePath)\s+['\"]?([^'\";\s)]+)", re.I)


def _read_codex(entries: List[Dict[str, Any]], cwd: str) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    conversation: List[Dict[str, str]] = []
    supplied: Dict[str, Any] = {}
    files: List[str] = []
    for entry in entries:
        if entry.get("type") == "session_meta" and not cwd:
            cwd = (entry.get("payload") or {}).get("cwd") or ""
        if entry.get("type") != "response_item":
            continue  # event_msg, turn_context, token counts: bookkeeping
        item = entry.get("payload") or {}
        kind = item.get("type")
        if kind == "message" and item.get("role") in ("user", "assistant"):
            # developer messages hold Codex's own system prompt, skills and permissions text
            parts = [c.get("text", "") for c in item.get("content") or []
                     if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")]
            text = "\n".join(parts)
            for pattern in _CODEX_INJECTED:
                text = pattern.sub(" ", text)
            text = _clean(text)
            if text:
                conversation.append({"role": item["role"], "content": text})
        elif kind in ("function_call", "custom_tool_call", "local_shell_call"):
            name = str(item.get("name") or "")
            raw = item.get("arguments") if "arguments" in item else item.get("input")
            args = raw
            if isinstance(raw, str) and raw.strip().startswith("{"):
                try:
                    args = json.loads(raw)  # function_call arguments are a JSON-encoded object
                except json.JSONDecodeError:
                    args = raw
            if isinstance(args, dict):
                body = "\n".join(str(v) for v in args.values() if isinstance(v, str))
                if _SAVE_TOOL.search(name):
                    _merge_supplied(supplied, args.get("handoff_json"))
            else:
                body = args if isinstance(args, str) else json.dumps(args or item.get("action") or "")
            # Edited paths only; tool output (custom_tool_call_output etc.) is never read.
            for target in _PATCH_FILE.findall(body) + _SHELL_WRITE.findall(body):
                rel = _relative(target, cwd) if cwd else target
                if rel not in files:
                    files.append(rel)
    return conversation, _with_files(supplied, files)


def _with_files(supplied: Dict[str, Any], files: List[str]) -> Dict[str, Any]:
    if files:
        supplied["files"] = supplied.get("files", []) + [f for f in files if f not in supplied.get("files", [])]
    return supplied


def _merge_supplied(into: Dict[str, Any], raw: Any) -> None:
    if not raw:
        return
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return
    if not isinstance(raw, dict):
        return
    for key in ("goal", "current_state"):
        if raw.get(key):
            into[key] = raw[key]  # the latest save wins
    for key in ("decisions", "open_tasks", "key_facts", "files"):
        value = raw.get(key)
        if value:
            into.setdefault(key, []).extend(value if isinstance(value, list) else [value])

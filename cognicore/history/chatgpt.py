"""Incremental reader for ChatGPT JSON exports and numbered ZIP members."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath

from cognicore.memory.redact import redact_secrets

MAX_ITEM = 32 * 1024 * 1024
MAX_ARCHIVE = 4 * 1024**3
_MEMBER = re.compile(r"conversations(?:[-_]\d+)?\.json", re.I)
_SECRET_FIELD = re.compile(
    r"password|passwd|passphrase|secret|client[_-]?secret|private[_-]?key|authorization|api[_-]?key|"
    r"(?:(?:access|refresh|auth)[_-]?)?token",
    re.I,
)


def digest(value):
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def clean(value):
    """Redact structured fields and free text before either storage or indexing."""
    if isinstance(value, str):
        value = value.encode("utf-8", "replace").decode("utf-8")
        return redact_secrets(value)[0]
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {
            clean(key): "[REDACTED:credential]" if _SECRET_FIELD.fullmatch(key) else clean(item)
            for key, item in value.items()
        }
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def event_time(value):
    """Normalize explicit timestamps to Unix seconds. Unknown stays null."""
    try:
        if type(value) in (int, float):
            if not math.isfinite(value):
                return None
            datetime.fromtimestamp(value, timezone.utc)
            return float(value)
        if isinstance(value, str):
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return date.timestamp() if date.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _documents(stream):
    """Decode one array element at a time, retaining at most one bounded record."""
    decoder = json.JSONDecoder()
    buffer = ""
    eof = False

    def fill():
        nonlocal buffer, eof
        chunk = stream.read(64 * 1024)
        buffer += chunk
        eof = not chunk

    def peek():
        nonlocal buffer
        while True:
            buffer = buffer.lstrip()
            if buffer or eof:
                return buffer[:1]
            fill()

    def value():
        nonlocal buffer
        peek()
        while True:
            if len(buffer) > MAX_ITEM:
                raise ValueError("A conversation exceeds the record size limit.")
            try:
                item, end = decoder.raw_decode(buffer)
                # Wait for a delimiter when a number might span two chunks.
                if end < len(buffer) or eof:
                    buffer = buffer[end:]
                    return item
            except json.JSONDecodeError:
                if eof:
                    raise ValueError("Invalid conversation JSON.") from None
            fill()

    if peek() == "[":
        buffer = buffer[1:]
        if peek() != "]":
            while True:
                yield value()
                if peek() != ",":
                    break
                buffer = buffer[1:]
        if peek() != "]":
            raise ValueError("Invalid conversation array.")
        buffer = buffer[1:]
    elif peek() == "{":
        root = value()
        if isinstance(root.get("conversations"), list):
            yield from root["conversations"]
        else:
            yield root
    else:
        raise ValueError("Expected a conversation object or array.")
    if peek():
        raise ValueError("Unexpected data after conversation JSON.")


def _archive_documents(source):
    if not zipfile.is_zipfile(source):
        source.seek(0)
        stream = io.TextIOWrapper(source, encoding="utf-8-sig")
        try:
            yield from _documents(stream)
        finally:
            stream.detach()
        return
    with zipfile.ZipFile(source) as archive:
        members = [info for info in archive.infolist()
                   if not info.is_dir() and _MEMBER.fullmatch(PurePosixPath(info.filename).name)]
        if not members:
            raise ValueError("ZIP contains no supported ChatGPT conversation JSON.")
        if sum(info.file_size for info in members) > MAX_ARCHIVE:
            raise ValueError("Conversation data exceeds the archive size limit.")
        for member in sorted(members, key=lambda info: info.filename):
            with archive.open(member) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig") as stream:
                yield from _documents(stream)


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, map(_text, content)))
    if isinstance(content, dict):
        return "\n".join(filter(None, (_text(content[key]) for key in ("parts", "text", "result", "output")
                                        if key in content)))
    return ""


def _message(raw, node, node_id, parent):
    if not isinstance(raw, dict):
        raw = {}
    author = raw.get("author")
    author = author if isinstance(author, dict) else {}
    metadata = raw.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    content = raw.get("content")
    role = author.get("role") or raw.get("role")
    original_id = raw.get("id")
    timestamp = raw.get("create_time")
    item = {
        "original_message_id": original_id if isinstance(original_id, str) else None,
        "node_id": node_id,
        "parent_node_id": parent,
        "role": role if isinstance(role, str) else None,
        "speaker": author.get("name"),
        "recipient": raw.get("recipient"),
        "tool_interaction": {key: raw.get(key, metadata.get(key))
                             for key in ("tool_calls", "tool_call_id", "tool_name", "execution_output", "exit_code")
                             if key in raw or key in metadata},
        "original_created_at": timestamp,
        "event_time": event_time(timestamp),
        "content": content,
        "text": _text(content),
        "attachments": {
            "references": metadata.get("attachments", raw.get("attachments", [])),
            "content_parts": [part for part in content.get("parts", [])
                              if isinstance(part, dict) and any(key in part for key in
                                  ("asset_pointer", "file_id", "image_url", "audio_asset_pointer"))]
            if isinstance(content, dict) and isinstance(content.get("parts"), list) else [],
        },
        "parsing_status": "parsed" if isinstance(content, (str, list, dict)) and isinstance(role, str)
        else "incomplete",
    }
    item["content_hash"] = digest({"source": raw, "node": node_id, "parent": node.get("parent", parent)})
    return item


class ChatGPTAdapter:
    platform = "chatgpt"

    def conversations(self, source):
        for raw in _archive_documents(source):
            if not isinstance(raw, dict):
                yield None
                continue
            mapping, messages = raw.get("mapping"), raw.get("messages")
            items = []
            if isinstance(mapping, dict):
                for node_id, node in mapping.items():
                    if isinstance(node, dict):
                        if node.get("message") is not None or node.get("parent") is not None:
                            items.append(_message(node.get("message"), node, node_id, node.get("parent")))
                    else:
                        items.append(_message(None, {}, node_id, None))
            elif isinstance(messages, list):
                for index, message in enumerate(messages):
                    items.append(_message(message, {}, str(index), str(index - 1) if index else None))
            else:
                yield None
                continue
            original_id = raw.get("conversation_id") or raw.get("id")
            original_id = original_id if isinstance(original_id, str) else None
            timestamp = raw.get("create_time")
            title = raw.get("title")
            conversation = {
                "original_conversation_id": original_id,
                "title": title if isinstance(title, str) else "",
                "original_created_at": timestamp,
                "event_time": event_time(timestamp),
                "attachments": raw.get("attachments", []),
                "current_node": raw.get("current_node"),
                "parsing_status": "parsed" if original_id and items and all(
                    item["parsing_status"] == "parsed" for item in items) else "incomplete",
            }
            conversation["content_hash"] = digest(conversation)
            yield conversation, items

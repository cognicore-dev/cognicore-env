"""
Secret detection and redaction — zero LLM calls.

Session saves and context compression copy conversation text into durable
memory that is later shown to other agents. Anything that looks like a
credential is replaced with ``[REDACTED:<kind>]`` before it is stored, so one
pasted key does not end up in every future resume brief.

Detection is pattern based (well-known key prefixes, PEM blocks, JWTs,
``password=...`` style assignments, credentials in URLs) plus a conservative
high-entropy check for long mixed-case tokens. Commit hashes, versions, ids and
file paths are deliberately left alone.
"""

import math
import re
from collections import Counter
from typing import List, Tuple

# (kind, pattern). Order matters: specific prefixes before generic rules.
_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)")),
    ("api_key", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}")),
    ("api_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer_token", re.compile(r"(?i)(?<=\bbearer\s)[A-Za-z0-9._~+/\-]{20,}=*")),
]

# Credentials embedded in URLs: scheme://user:PASSWORD@host
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]+)(@)", re.I)

# password = hunter2 / api_key: "abc..." / token=... (value only is replaced)
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|passphrase|secret|client[_-]?secret|api[_-]?key|"
    r"access[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|token)"
    r"(\s*[:=]\s*[\"']?)([^\s\"',;]{6,})"
)

# "the password is hunter2"
_PASSWORD_IS = re.compile(r"(?i)\b(password|passphrase)(\s+(?:is|was)\s+[\"']?)([^\s\"',;]{4,})")

_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}")


def _entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _looks_random(token: str) -> bool:
    """Long, mixed upper/lower/digit and high entropy. Hex hashes and words are excluded."""
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    has_digit = any(c.isdigit() for c in token)
    if not (has_upper and has_lower and has_digit):
        return False
    if "/" in token and token.count("/") > 2:  # file paths
        return False
    return _entropy(token) >= 4.0


def redact_secrets(text: str) -> Tuple[str, int]:
    """Return ``(redacted_text, number_of_redactions)``."""
    if not text:
        return text, 0
    count = 0

    def _sub(kind: str):
        def repl(_m):
            nonlocal count
            count += 1
            return f"[REDACTED:{kind}]"
        return repl

    for kind, pattern in _PATTERNS:
        text = pattern.sub(_sub(kind), text)

    def _keep_prefix(kind: str):
        def repl(m):
            nonlocal count
            if m.group(3).startswith("[REDACTED"):
                return m.group(0)
            count += 1
            return f"{m.group(1)}{m.group(2)}[REDACTED:{kind}]"
        return repl

    def _url_repl(m):
        nonlocal count
        count += 1
        return f"{m.group(1)}[REDACTED:password]{m.group(3)}"

    text = _URL_CREDENTIALS.sub(_url_repl, text)
    text = _ASSIGNMENT.sub(_keep_prefix("credential"), text)
    text = _PASSWORD_IS.sub(_keep_prefix("password"), text)

    def _entropy_repl(m):
        nonlocal count
        if _looks_random(m.group(0)):
            count += 1
            return "[REDACTED:token]"
        return m.group(0)

    text = _LONG_TOKEN.sub(_entropy_repl, text)
    return text, count


def contains_secret(text: str) -> bool:
    return redact_secrets(text)[1] > 0

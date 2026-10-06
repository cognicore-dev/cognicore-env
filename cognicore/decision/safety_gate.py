"""Deterministic Safety Gate for CogniCore Decision Layer.

Enforces strict, non-probabilistic security policies BEFORE any statistical
or neural decision model is evaluated.

Addresses critical safety findings:
  1. Safety must not be probabilistic — destructive actions cannot be
     gated solely by confidence thresholds.
  2. Protects against adversarial manipulation of decision probabilities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class SafetyVerdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_HUMAN = "require_human"


@dataclass
class SafetyCheckResult:
    passed: bool
    verdict: SafetyVerdict
    reason: str = ""
    matched_rule: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "matched_rule": self.matched_rule,
        }


class DeterministicSafetyGate:
    """Zero-dependency deterministic safety layer that runs before probabilities.

    Checks for:
      - Destructive system commands (rm -rf, format, dd, etc.)
      - Destructive database statements (DROP TABLE, TRUNCATE, etc.)
      - Exfiltration of secrets or credentials
      - Irreversible external actions requiring human-in-the-loop
    """

    # Destructive shell command patterns
    _DESTRUCTIVE_SHELL_PATTERNS = [
        (r"\brm\s+.*(-[a-zA-Z]*r[a-zA-Z]*|--recursive)\b.*[/~*]", "Recursive root/home file deletion"),
        (r"\bmkfs\b", "Filesystem format"),
        (r"\bdd\s+if=.*of=(?:/dev/|\\\\)", "Direct disk block overwrite"),
        (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", "Fork bomb execution"),
        (r"\b(shutdown|reboot|poweroff|init\s+0)\b", "System shutdown/reboot command"),
    ]

    # Destructive SQL patterns
    _DESTRUCTIVE_SQL_PATTERNS = [
        (r"\bDROP\s+(DATABASE|SCHEMA|TABLE)\b", "Destructive SQL DROP statement"),
        (r"\bTRUNCATE\s+TABLE\b", "Destructive SQL TRUNCATE statement"),
        (r"\bDELETE\s+FROM\s+\w+\s*;?\s*$", "Unconstrained SQL DELETE without WHERE clause"),
    ]

    # Sensitive data exfiltration patterns
    _EXFILTRATION_PATTERNS = [
        (r"\b(dump|print|reveal|export)\s+.*(api_key|secret|password|token|private_key|aws_secret)\b", "Secret exfiltration attempt"),
        (r"\b(cat|type)\s+.*(\.env|id_rsa|id_ed25519|credentials)\b", "Credential file access"),
    ]

    def __init__(
        self,
        custom_rules: Optional[List[Callable[[str, Dict[str, Any]], Optional[str]]]] = None,
        require_human_on_destructive: bool = True,
    ) -> None:
        self.custom_rules = custom_rules or []
        self.require_human_on_destructive = require_human_on_destructive

    def check(self, action: str, context: Optional[Dict[str, Any]] = None) -> SafetyCheckResult:
        """Run deterministic policy checks on the proposed action."""
        context = context or {}
        text = str(action).strip()

        # 1. Shell commands check
        for pattern, rule_name in self._DESTRUCTIVE_SHELL_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return SafetyCheckResult(
                    passed=False,
                    verdict=SafetyVerdict.BLOCK,
                    reason=f"Blocked dangerous system command: {rule_name}",
                    matched_rule=rule_name,
                )

        # 2. SQL destructive operations check
        for pattern, rule_name in self._DESTRUCTIVE_SQL_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return SafetyCheckResult(
                    passed=False,
                    verdict=SafetyVerdict.BLOCK,
                    reason=f"Blocked dangerous database operation: {rule_name}",
                    matched_rule=rule_name,
                )

        # 3. Exfiltration check
        for pattern, rule_name in self._EXFILTRATION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return SafetyCheckResult(
                    passed=False,
                    verdict=SafetyVerdict.BLOCK,
                    reason=f"Blocked potential security exfiltration: {rule_name}",
                    matched_rule=rule_name,
                )

        # 4. Check custom policy functions
        for custom_rule in self.custom_rules:
            violation = custom_rule(text, context)
            if violation:
                return SafetyCheckResult(
                    passed=False,
                    verdict=SafetyVerdict.BLOCK,
                    reason=f"Custom policy violation: {violation}",
                    matched_rule="custom_policy",
                )

        # 5. Irreversible action gate check (e.g. git push --force, payment, release)
        if re.search(r"\b(git\s+push\s+.*--force|deploy\s+prod|release\s+live|pay\b|transfer_funds)\b", text, re.IGNORECASE):
            if self.require_human_on_destructive:
                return SafetyCheckResult(
                    passed=False,
                    verdict=SafetyVerdict.REQUIRE_HUMAN,
                    reason="Irreversible action requires human approval",
                    matched_rule="human_approval_required",
                )

        return SafetyCheckResult(
            passed=True,
            verdict=SafetyVerdict.ALLOW,
            reason="Action passed deterministic safety gate",
            matched_rule="none",
        )

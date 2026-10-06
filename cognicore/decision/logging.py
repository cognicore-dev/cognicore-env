"""Decision Logging for CogniCore Decision Layer.

Integrates with CogniCore's EventRecorder and EventStore to persist every
decision evaluation, safety check, probability, uncertainty, and escalation
for auditing, calibration tracking, and offline model retraining.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from cognicore.decision.base import DecisionResult
from cognicore.decision.safety_gate import SafetyCheckResult


class DecisionLogger:
    """Logs decisions and outcomes with replay integration."""

    def __init__(self, recorder: Optional[Any] = None) -> None:
        self.recorder = recorder
        self._local_history: List[Dict[str, Any]] = []

    def log_decision(
        self,
        task_id: str,
        action_or_candidates: Any,
        decision_result: DecisionResult,
        safety_result: Optional[SafetyCheckResult] = None,
        step: int = 1,
    ) -> Dict[str, Any]:
        """Record a decision evaluation event."""
        payload = {
            "task_id": task_id,
            "timestamp": time.time(),
            "step": step,
            "input": action_or_candidates,
            "decision": decision_result.decision,
            "probability": decision_result.probability,
            "uncertainty": decision_result.uncertainty,
            "confidence_interval": decision_result.confidence_interval,
            "escalate": decision_result.escalate,
            "safety": safety_result.to_dict() if safety_result else None,
            "reasoning": decision_result.reasoning,
            "backend": decision_result.metadata.get("backend", "unknown"),
            "outcome": None,
        }

        self._local_history.append(payload)

        # Integrate with EventRecorder if present
        if self.recorder is not None:
            event_type = "decision_escalated" if decision_result.escalate else "decision_evaluated"
            if safety_result and not safety_result.passed:
                event_type = "immune_blocked"

            try:
                if hasattr(self.recorder, "record_simple"):
                    self.recorder.record_simple(
                        task_id=task_id,
                        event_type=event_type,
                        step=step,
                        input_text=str(action_or_candidates)[:500],
                        output_text=f"Decision: {decision_result.decision} (P={decision_result.probability:.3f}, Escalate={decision_result.escalate})",
                    )
            except Exception:
                pass

        return payload

    def record_outcome(self, task_id: str, outcome: bool) -> None:
        """Attach observed outcome (success/failure) to the corresponding decision."""
        for entry in reversed(self._local_history):
            if entry["task_id"] == task_id and entry["outcome"] is None:
                entry["outcome"] = outcome
                break

    def get_history(self) -> List[Dict[str, Any]]:
        """Return history of all recorded decisions."""
        return list(self._local_history)

    def get_calibration_dataset(self) -> Tuple[List[float], List[bool]]:
        """Extract paired (probability, outcome) for all decisions where outcome is known."""
        probs = []
        outcomes = []
        for entry in self._local_history:
            if entry["outcome"] is not None:
                probs.append(float(entry["probability"]))
                outcomes.append(bool(entry["outcome"]))
        return probs, outcomes

"""Probabilistic Cognitive Control Architecture (PCCA) Controller.

Orchestrates the 4-stage pipeline:
  1. Deterministic Safety Gate (Hard policy check; runs first)
  2. Probabilistic Decision Model (Calibrated Bayesian/neural estimate)
  3. Escalation Controller (Escalates to slow generative reasoning when uncertain)
  4. Decision Logger & Audit Replay (Persists telemetry for calibration & retraining)
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from cognicore.decision.base import DecisionBackend, DecisionResult
from cognicore.decision.beta_backend import MemoryBetaDecisionBackend
from cognicore.decision.calibration import (
    TemperatureScaler,
    brier_score,
    expected_calibration_error,
)
from cognicore.decision.logging import DecisionLogger
from cognicore.decision.safety_gate import DeterministicSafetyGate, SafetyVerdict


class ProbabilisticCognitiveControl:
    """Central engine for Probabilistic Cognitive Control in CogniCore."""

    def __init__(
        self,
        backend: Optional[DecisionBackend] = None,
        safety_gate: Optional[DeterministicSafetyGate] = None,
        logger: Optional[DecisionLogger] = None,
        scaler: Optional[TemperatureScaler] = None,
    ) -> None:
        self.backend = backend or MemoryBetaDecisionBackend()
        self.safety_gate = safety_gate or DeterministicSafetyGate()
        self.logger = logger or DecisionLogger()
        self.scaler = scaler or TemperatureScaler()

        # Telemetry
        self.total_decisions: int = 0
        self.fast_decisions: int = 0
        self.escalated_decisions: int = 0
        self.blocked_decisions: int = 0

    def evaluate_action(
        self,
        action: str,
        context: Dict[str, Any],
        task_id: str = "default",
        slow_reasoner: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> DecisionResult:
        """Evaluate an action through the PCCA pipeline."""
        self.total_decisions += 1

        # Stage 1: Deterministic Safety Gate (Hard policy, strictly non-probabilistic)
        safety = self.safety_gate.check(action, context)
        if not safety.passed:
            self.blocked_decisions += 1
            res = DecisionResult(
                decision=None,
                probability=0.0,
                uncertainty=0.0,
                escalate=False,
                reasoning=f"BLOCKED by safety gate: {safety.reason}",
                metadata={"safety_verdict": safety.verdict.value, "rule": safety.matched_rule},
            )
            self.logger.log_decision(task_id, action, res, safety)
            return res

        # Stage 2: Probabilistic Decision Model
        res = self.backend.score(action, context)

        # Stage 3: Apply learned temperature calibration
        calibrated_p = self.scaler.calibrate(res.probability)
        res.probability = calibrated_p

        # Stage 4: Escalation Controller
        if res.escalate and slow_reasoner is not None:
            self.escalated_decisions += 1
            t0 = time.perf_counter()
            slow_output = slow_reasoner(action, context)
            latency_ms = (time.perf_counter() - t0) * 1000
            res.metadata["slow_reasoner_invoked"] = True
            res.metadata["slow_reasoner_latency_ms"] = latency_ms
            res.metadata["slow_reasoner_output"] = slow_output
            res.decision = slow_output
            res.reasoning += f" -> Escalated to slow reasoner: {slow_output}"
        else:
            self.fast_decisions += 1

        self.logger.log_decision(task_id, action, res, safety)
        return res

    def choose_action(
        self,
        candidates: List[str],
        context: Dict[str, Any],
        task_id: str = "default",
        slow_reasoner: Optional[Callable[[List[str], Dict[str, Any]], str]] = None,
    ) -> DecisionResult:
        """Select the best candidate action through the PCCA pipeline."""
        self.total_decisions += 1

        # Stage 1: Filter out any candidates that violate deterministic safety
        safe_candidates = []
        for cand in candidates:
            safety = self.safety_gate.check(cand, context)
            if safety.passed:
                safe_candidates.append(cand)

        if not safe_candidates:
            self.blocked_decisions += 1
            res = DecisionResult(
                decision="",
                probability=0.0,
                uncertainty=0.0,
                escalate=False,
                reasoning="All candidates blocked by deterministic safety gate",
                metadata={"safety_verdict": "all_blocked"},
            )
            self.logger.log_decision(task_id, candidates, res)
            return res

        # Stage 2: Probabilistic choice over safe candidates
        res = self.backend.choose(safe_candidates, context)

        # Stage 3: Calibration
        res.probability = self.scaler.calibrate(res.probability)

        # Stage 4: Escalation if uncertain or close call
        if res.escalate and slow_reasoner is not None:
            self.escalated_decisions += 1
            slow_choice = slow_reasoner(safe_candidates, context)
            res.decision = slow_choice
            res.metadata["slow_reasoner_invoked"] = True
            res.reasoning += f" -> Escalated to slow reasoner: chosen '{slow_choice}'"
        else:
            self.fast_decisions += 1

        self.logger.log_decision(task_id, candidates, res)
        return res

    def record_outcome(self, task_id: str, success: bool) -> None:
        """Record the actual outcome of the task for online learning & calibration."""
        self.logger.record_outcome(task_id, success)

    def get_calibration_metrics(self) -> Dict[str, Any]:
        """Compute calibration metrics across historical decisions."""
        probs, outcomes = self.logger.get_calibration_dataset()
        if not probs:
            return {"ece": 0.0, "brier": 0.0, "samples": 0}

        return {
            "ece": round(expected_calibration_error(probs, outcomes), 4),
            "brier": round(brier_score(probs, outcomes), 4),
            "samples": len(probs),
            "fast_ratio": round(self.fast_decisions / max(1, self.total_decisions), 4),
            "escalation_ratio": round(self.escalated_decisions / max(1, self.total_decisions), 4),
            "blocked_ratio": round(self.blocked_decisions / max(1, self.total_decisions), 4),
        }

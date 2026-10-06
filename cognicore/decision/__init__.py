"""CogniCore Decision Layer — Probabilistic Cognitive Control Architecture (PCCA).

Provides:
  - DecisionBackend: Abstract interface with choose, score, and yes_no primitives.
  - DecisionResult: Typed probabilistic decision output with uncertainty and escalation flags.
  - MemoryBetaDecisionBackend: Zero-dependency Bayesian Beta-posterior backend.
  - DeterministicSafetyGate: Deterministic safety enforcement before probabilistic decisions.
  - TemperatureScaler, expected_calibration_error, brier_score: Calibration tooling.
  - DecisionLogger: Decision persistence integrated with audit replay.
  - ProbabilisticCognitiveControl: Central PCCA controller.
"""

from cognicore.decision.base import DecisionBackend, DecisionResult
from cognicore.decision.beta_backend import MemoryBetaDecisionBackend
from cognicore.decision.safety_gate import DeterministicSafetyGate, SafetyVerdict, SafetyCheckResult
from cognicore.decision.calibration import (
    TemperatureScaler,
    expected_calibration_error,
    maximum_calibration_error,
    brier_score,
    compute_reliability_diagram,
)
from cognicore.decision.logging import DecisionLogger
from cognicore.decision.controller import ProbabilisticCognitiveControl

__all__ = [
    "DecisionBackend",
    "DecisionResult",
    "MemoryBetaDecisionBackend",
    "DeterministicSafetyGate",
    "SafetyVerdict",
    "SafetyCheckResult",
    "TemperatureScaler",
    "expected_calibration_error",
    "maximum_calibration_error",
    "brier_score",
    "compute_reliability_diagram",
    "DecisionLogger",
    "ProbabilisticCognitiveControl",
]

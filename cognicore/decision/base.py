"""Base interfaces and data structures for CogniCore Decision Layer.

Part of the Probabilistic Cognitive Control Architecture (PCCA).
Separates fast, calibrated probabilistic control from slow generative reasoning.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DecisionResult:
    """Standardized output of a decision primitive."""

    decision: Any
    """The concrete decision: selected candidate (str), score (float), or bool."""

    probability: float
    """Posterior mean probability or calibrated confidence in [0.0, 1.0]."""

    uncertainty: float
    """Uncertainty measure (e.g., credible interval width or standard deviation)."""

    confidence_interval: Tuple[float, float] = (0.0, 1.0)
    """Credible interval (e.g. 95% interval [lower, upper])."""

    escalate: bool = False
    """True if confidence is low, uncertainty is wide, or policy demands slow reasoning."""

    reasoning: str = ""
    """Human-readable explanation of why this decision was made."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Backend-specific details (sample sizes, priors, execution time, etc.)."""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "probability": round(self.probability, 4),
            "uncertainty": round(self.uncertainty, 4),
            "confidence_interval": [
                round(self.confidence_interval[0], 4),
                round(self.confidence_interval[1], 4),
            ],
            "escalate": self.escalate,
            "reasoning": self.reasoning,
            "metadata": self.metadata,
        }


class DecisionBackend(ABC):
    """Abstract interface for pluggable decision backends.

    Provides three core primitives:
      1. choose — pick the best action/candidate from a discrete list
      2. score  — estimate probability of success for a proposed action
      3. yes_no — evaluate a binary question / approval gate
    """

    @abstractmethod
    def choose(
        self,
        candidates: List[str],
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> DecisionResult:
        """Select the highest-confidence candidate action."""
        pass

    @abstractmethod
    def score(
        self,
        action: str,
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> DecisionResult:
        """Score the expected success probability of a proposed action."""
        pass

    @abstractmethod
    def yes_no(
        self,
        question: str,
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> DecisionResult:
        """Evaluate a binary decision or verification question."""
        pass

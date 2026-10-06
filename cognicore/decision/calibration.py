"""Calibration Tooling for CogniCore Decision Layer.

Implements:
  - Expected Calibration Error (ECE)
  - Maximum Calibration Error (MCE)
  - Brier Score
  - Reliability Diagram Binning
  - Zero-dependency Temperature Scaling
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def _logit(p: float, eps: float = 1e-6) -> float:
    """Compute logit (log-odds) with numerical clipping."""
    p_clipped = max(eps, min(1.0 - eps, p))
    return math.log(p_clipped / (1.0 - p_clipped))


def _sigmoid(z: float) -> float:
    """Compute sigmoid function with overflow protection."""
    if z >= 30.0:
        return 1.0
    if z <= -30.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def brier_score(probabilities: List[float], outcomes: List[bool]) -> float:
    """Compute mean squared error between probabilities and binary outcomes."""
    if not probabilities or len(probabilities) != len(outcomes):
        return 0.0
    n = len(probabilities)
    return sum((float(p) - float(y)) ** 2 for p, y in zip(probabilities, outcomes)) / n


def expected_calibration_error(
    probabilities: List[float],
    outcomes: List[bool],
    n_bins: int = 10,
) -> float:
    """Compute Expected Calibration Error (ECE) across equal-width bins.

    ECE = sum_m (|B_m| / N) * |acc(B_m) - conf(B_m)|
    """
    if not probabilities or len(probabilities) != len(outcomes):
        return 0.0

    n = len(probabilities)
    bin_size = 1.0 / n_bins
    ece = 0.0

    for i in range(n_bins):
        low = i * bin_size
        high = (i + 1) * bin_size
        # Include upper boundary in last bin
        if i == n_bins - 1:
            bin_indices = [
                idx for idx, p in enumerate(probabilities) if low <= p <= 1.0
            ]
        else:
            bin_indices = [
                idx for idx, p in enumerate(probabilities) if low <= p < high
            ]

        bin_count = len(bin_indices)
        if bin_count == 0:
            continue

        bin_conf = sum(probabilities[idx] for idx in bin_indices) / bin_count
        bin_acc = sum(1.0 if outcomes[idx] else 0.0 for idx in bin_indices) / bin_count

        ece += (bin_count / n) * abs(bin_acc - bin_conf)

    return ece


def maximum_calibration_error(
    probabilities: List[float],
    outcomes: List[bool],
    n_bins: int = 10,
) -> float:
    """Compute Maximum Calibration Error (MCE)."""
    if not probabilities or len(probabilities) != len(outcomes):
        return 0.0

    bin_size = 1.0 / n_bins
    mce = 0.0

    for i in range(n_bins):
        low = i * bin_size
        high = (i + 1) * bin_size
        if i == n_bins - 1:
            bin_indices = [
                idx for idx, p in enumerate(probabilities) if low <= p <= 1.0
            ]
        else:
            bin_indices = [
                idx for idx, p in enumerate(probabilities) if low <= p < high
            ]

        bin_count = len(bin_indices)
        if bin_count == 0:
            continue

        bin_conf = sum(probabilities[idx] for idx in bin_indices) / bin_count
        bin_acc = sum(1.0 if outcomes[idx] else 0.0 for idx in bin_indices) / bin_count
        gap = abs(bin_acc - bin_conf)
        if gap > mce:
            mce = gap

    return mce


def compute_reliability_diagram(
    probabilities: List[float],
    outcomes: List[bool],
    n_bins: int = 10,
) -> Dict[str, Any]:
    """Generate binned statistics for reliability diagrams."""
    if not probabilities:
        return {"bins": [], "ece": 0.0, "brier": 0.0}

    bin_size = 1.0 / n_bins
    bins_data = []

    for i in range(n_bins):
        low = i * bin_size
        high = (i + 1) * bin_size
        center = (low + high) / 2.0
        if i == n_bins - 1:
            bin_indices = [
                idx for idx, p in enumerate(probabilities) if low <= p <= 1.0
            ]
        else:
            bin_indices = [
                idx for idx, p in enumerate(probabilities) if low <= p < high
            ]

        count = len(bin_indices)
        if count > 0:
            avg_conf = sum(probabilities[idx] for idx in bin_indices) / count
            accuracy = sum(1.0 if outcomes[idx] else 0.0 for idx in bin_indices) / count
        else:
            avg_conf = center
            accuracy = 0.0

        bins_data.append({
            "bin_range": [round(low, 2), round(high, 2)],
            "bin_center": round(center, 2),
            "count": count,
            "confidence": round(avg_conf, 4),
            "accuracy": round(accuracy, 4),
        })

    return {
        "bins": bins_data,
        "ece": round(expected_calibration_error(probabilities, outcomes, n_bins), 4),
        "mce": round(maximum_calibration_error(probabilities, outcomes, n_bins), 4),
        "brier": round(brier_score(probabilities, outcomes), 4),
        "total_samples": len(probabilities),
    }


class TemperatureScaler:
    """Zero-dependency temperature scaling to calibrate probabilities."""

    def __init__(self, temperature: float = 1.0) -> None:
        self.temperature = max(0.01, float(temperature))

    def calibrate(self, probability: float) -> float:
        """Apply learned temperature scaling to probability."""
        logit = _logit(probability)
        scaled_logit = logit / self.temperature
        return _sigmoid(scaled_logit)

    def fit(self, probabilities: List[float], outcomes: List[bool]) -> float:
        """Fit temperature parameter via 1D grid search minimizing log-loss."""
        if not probabilities or len(probabilities) < 3:
            return self.temperature

        # Evaluate candidate temperatures in [0.1, 5.0]
        best_t = 1.0
        best_nll = float("inf")

        logits = [_logit(p) for p in probabilities]
        targets = [1.0 if y else 0.0 for y in outcomes]

        for step in range(1, 100):
            t = step * 0.05
            nll = 0.0
            for z, y in zip(logits, targets):
                p_cal = _sigmoid(z / t)
                p_cal = max(1e-7, min(1.0 - 1e-7, p_cal))
                nll -= (y * math.log(p_cal) + (1.0 - y) * math.log(1.0 - p_cal))

            if nll < best_nll:
                best_nll = nll
                best_t = t

        self.temperature = best_t
        return self.temperature

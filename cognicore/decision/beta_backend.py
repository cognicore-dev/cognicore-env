"""Memory-conditioned Beta-posterior decision backend.

Zero-dependency statistical decision layer that conditions directly on
CogniCore's episodic memory and historical task outcomes.

Computes a closed-form Beta posterior P(success | context, action) with
principled Bayesian uncertainty. When observations are few, the posterior
is wide and automatically triggers escalation to slow reasoning (LLMs).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from cognicore.decision.base import DecisionBackend, DecisionResult
from cognicore.memory.base import MemoryBackend, MemoryEntry


class MemoryBetaDecisionBackend(DecisionBackend):
    """Bayesian decision backend using Beta-Binomial conjugate update over memory.

    Parameters
    ----------
    memory : MemoryBackend or None
        The CogniCore memory backend to retrieve historical outcomes from.
    prior_alpha : float
        Prior successes (default 1.0 for uniform prior / Laplace smoothing).
    prior_beta : float
        Prior failures (default 1.0 for uniform prior).
    min_observations : float
        Minimum effective sample size required before trusting the fast decision.
    max_uncertainty : float
        Maximum 95% credible interval width permitted before triggering escalation.
    confidence_threshold : float
        Posterior probability required to approve an action or 'yes' decision.
    ambiguity_zone : tuple of (float, float)
        Interval [low, high] where probability is too ambiguous to decide without escalation.
    """

    def __init__(
        self,
        memory: Optional[MemoryBackend] = None,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
        min_observations: float = 2.0,
        max_uncertainty: float = 0.45,
        confidence_threshold: float = 0.70,
        ambiguity_zone: Tuple[float, float] = (0.35, 0.65),
    ) -> None:
        self.memory = memory
        self.prior_alpha = prior_alpha
        self.prior_beta = prior_beta
        self.min_observations = min_observations
        self.max_uncertainty = max_uncertainty
        self.confidence_threshold = confidence_threshold
        self.ambiguity_zone = ambiguity_zone

    def _compute_posterior(
        self,
        query: str,
        category: Optional[str] = None,
        entries: Optional[List[MemoryEntry]] = None,
    ) -> Tuple[float, float, float, Tuple[float, float], float]:
        """Compute (mean, variance, uncertainty_width, (ci_low, ci_high), effective_n)."""
        success_weights = 0.0
        failure_weights = 0.0

        items: List[Tuple[MemoryEntry, float]] = []
        if entries is not None:
            items = [(e, 1.0) for e in entries]
        elif self.memory is not None:
            results = self.memory.search(
                query=query,
                top_k=15,
                category=category if category and category != "default" else None,
            )
            max_s = max((r.score for r in results), default=1.0) if results else 1.0
            max_s = max(max_s, 1e-6)

            for r in results:
                norm_score = max(0.0, min(1.0, r.score / max_s))
                items.append((r.entry, norm_score))

            # If search gave few results, supplement with category matches with lower baseline score
            if len(items) < 3 and category and category != "default":
                cat_entries = self.memory.get_by_category(category, top_k=5)
                seen_ids = {e.entry_id for e, _ in items if e.entry_id}
                for ce in cat_entries:
                    if ce.entry_id not in seen_ids:
                        items.append((ce, 0.4))

        for e, sim_score in items:
            # Determine outcome
            is_correct = e.correct
            if is_correct is None:
                # Check metadata or memory_type
                if e.memory_type == "failure":
                    is_correct = False
                elif "reward" in e.metadata:
                    is_correct = float(e.metadata["reward"]) > 0.0

            if is_correct is None:
                continue

            # Skip entries with low similarity
            if sim_score < 0.40:
                continue

            # Weight by normalized similarity (sharp power) and recency relevance
            rel = max(0.2, min(1.0, float(getattr(e, "relevance", 1.0))))
            weight = (sim_score ** 6) * rel
            weight = max(0.01, min(1.0, weight))

            if is_correct:
                success_weights += weight
            else:
                failure_weights += weight

        effective_n = success_weights + failure_weights

        # Bayesian update: Beta(alpha_0 + s, beta_0 + f)
        alpha = self.prior_alpha + success_weights
        beta = self.prior_beta + failure_weights

        # Posterior mean and variance
        mean = alpha / (alpha + beta)
        variance = (alpha * beta) / ((alpha + beta) ** 2 * (alpha + beta + 1.0))
        std_dev = math.sqrt(variance)

        # 95% Credible interval & uncertainty width (2 * std_dev)
        ci_half_width = 1.96 * std_dev
        ci_low = max(0.0, mean - ci_half_width)
        ci_high = min(1.0, mean + ci_half_width)
        uncertainty = 2.0 * std_dev

        return mean, variance, uncertainty, (ci_low, ci_high), effective_n

    def score(
        self,
        action: str,
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> DecisionResult:
        """Estimate success probability P(success | context, action) with uncertainty."""
        category = context.get("category")
        task = context.get("task", "")
        query = f"{task} {action}".strip()

        mean, variance, uncertainty, ci, eff_n = self._compute_posterior(
            query=query, category=category
        )

        # Escalation criteria:
        # 1. Effective observations too low -> high epistemic uncertainty
        # 2. Credible interval too wide
        # 3. Probability falls in ambiguous zone with moderate data
        escalate = False
        reasons = []

        if eff_n < self.min_observations:
            escalate = True
            reasons.append(f"Insufficient history (effective observations = {eff_n:.1f} < {self.min_observations})")

        if uncertainty > self.max_uncertainty:
            escalate = True
            reasons.append(f"High uncertainty (CI width = {uncertainty:.2f} > {self.max_uncertainty})")

        if self.ambiguity_zone[0] <= mean <= self.ambiguity_zone[1] and eff_n < 8.0:
            escalate = True
            reasons.append(f"Posterior mean {mean:.2f} falls in ambiguity zone {self.ambiguity_zone}")

        if not escalate:
            reasons.append(f"Confident estimate based on {eff_n:.1f} effective observations")

        return DecisionResult(
            decision=mean,
            probability=mean,
            uncertainty=uncertainty,
            confidence_interval=ci,
            escalate=escalate,
            reasoning="; ".join(reasons),
            metadata={
                "backend": "MemoryBetaDecisionBackend",
                "effective_n": eff_n,
                "posterior_variance": variance,
            },
        )

    def choose(
        self,
        candidates: List[str],
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> DecisionResult:
        """Select best candidate or escalate if choice is ambiguous or uncertain."""
        if not candidates:
            return DecisionResult(
                decision="",
                probability=0.0,
                uncertainty=1.0,
                escalate=True,
                reasoning="No candidates provided",
            )

        category = context.get("category")
        task = context.get("task", "")

        candidate_scores = []
        for cand in candidates:
            query = f"{task} {cand}".strip()
            mean, variance, uncertainty, ci, eff_n = self._compute_posterior(
                query=query, category=category
            )
            candidate_scores.append({
                "candidate": cand,
                "mean": mean,
                "uncertainty": uncertainty,
                "ci": ci,
                "eff_n": eff_n,
            })

        # Rank candidates by posterior mean descending
        candidate_scores.sort(key=lambda x: x["mean"], reverse=True)
        best = candidate_scores[0]

        # Check for ambiguity with runner-up
        is_tied = False
        if len(candidate_scores) > 1:
            second_best = candidate_scores[1]
            diff = best["mean"] - second_best["mean"]
            # If scores are close within statistical uncertainty
            if diff < 0.10 and (best["uncertainty"] > 0.20 or second_best["uncertainty"] > 0.20):
                is_tied = True

        escalate = False
        reasons = []

        if best["eff_n"] < self.min_observations:
            escalate = True
            reasons.append(f"Sparse history for best candidate (n={best['eff_n']:.1f})")

        if best["uncertainty"] > self.max_uncertainty:
            escalate = True
            reasons.append(f"High uncertainty for best candidate ({best['uncertainty']:.2f})")

        if is_tied:
            escalate = True
            reasons.append("Close race between top candidates with overlapping credible intervals")

        if not escalate:
            reasons.append(f"Selected '{best['candidate']}' with confidence {best['mean']:.2f}")

        return DecisionResult(
            decision=best["candidate"],
            probability=best["mean"],
            uncertainty=best["uncertainty"],
            confidence_interval=best["ci"],
            escalate=escalate,
            reasoning="; ".join(reasons),
            metadata={
                "backend": "MemoryBetaDecisionBackend",
                "all_scores": [
                    {"candidate": c["candidate"], "p": round(c["mean"], 3), "uncertainty": round(c["uncertainty"], 3)}
                    for c in candidate_scores
                ],
                "effective_n": best["eff_n"],
            },
        )

    def yes_no(
        self,
        question: str,
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> DecisionResult:
        """Evaluate binary approval question."""
        category = context.get("category")
        mean, variance, uncertainty, ci, eff_n = self._compute_posterior(
            query=question, category=category
        )

        approved = mean >= self.confidence_threshold
        escalate = False
        reasons = []

        if eff_n < self.min_observations:
            escalate = True
            reasons.append(f"Insufficient history for binary decision (n={eff_n:.1f})")

        if uncertainty > self.max_uncertainty:
            escalate = True
            reasons.append(f"Uncertainty too wide ({uncertainty:.2f})")

        if self.ambiguity_zone[0] <= mean <= self.ambiguity_zone[1]:
            escalate = True
            reasons.append(f"Probability {mean:.2f} in ambiguity band")

        decision_str = "YES" if approved else "NO"
        if not escalate:
            reasons.append(f"Determined {decision_str} (P={mean:.2f})")

        return DecisionResult(
            decision=approved,
            probability=mean,
            uncertainty=uncertainty,
            confidence_interval=ci,
            escalate=escalate,
            reasoning="; ".join(reasons),
            metadata={
                "backend": "MemoryBetaDecisionBackend",
                "effective_n": eff_n,
            },
        )

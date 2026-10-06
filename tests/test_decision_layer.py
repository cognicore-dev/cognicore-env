"""Tests for CogniCore Decision Layer & Probabilistic Cognitive Control (PCCA).

Verifies:
  1. DeterministicSafetyGate runs first and strictly blocks destructive actions.
  2. MemoryBetaDecisionBackend computes correct Beta posteriors with zero dependencies.
  3. Escalation triggers when data is sparse or uncertainty is wide.
  4. DecisionBackend primitives (choose, score, yes_no).
  5. Calibration tooling (ECE, Brier score, TemperatureScaler).
  6. DecisionLogger tracking and outcome evaluation.
  7. ProbabilisticCognitiveControl end-to-end pipeline.
"""

import math
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cognicore.memory.tfidf_backend import TFIDFMemoryBackend
from cognicore.memory.base import MemoryEntry
from cognicore.decision import (
    DecisionBackend,
    DecisionResult,
    MemoryBetaDecisionBackend,
    DeterministicSafetyGate,
    SafetyVerdict,
    TemperatureScaler,
    expected_calibration_error,
    brier_score,
    compute_reliability_diagram,
    DecisionLogger,
    ProbabilisticCognitiveControl,
)


# ======================================================================
# 1. Deterministic Safety Gate
# ======================================================================

class TestDeterministicSafetyGate:
    """Verify safety gate is deterministic and blocks destructive actions."""

    def test_blocks_destructive_shell_commands(self):
        gate = DeterministicSafetyGate()
        dangerous_cmds = [
            "rm -rf /",
            "rm -f -r ~",
            "mkfs /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
            ":(){ :|:& };:",
            "shutdown -h now",
        ]
        for cmd in dangerous_cmds:
            res = gate.check(cmd)
            assert not res.passed, f"Should have blocked: {cmd}"
            assert res.verdict == SafetyVerdict.BLOCK

    def test_blocks_destructive_sql_statements(self):
        gate = DeterministicSafetyGate()
        dangerous_sql = [
            "DROP TABLE users;",
            "DROP DATABASE production;",
            "TRUNCATE TABLE accounts",
            "DELETE FROM orders",
        ]
        for sql in dangerous_sql:
            res = gate.check(sql)
            assert not res.passed, f"Should have blocked: {sql}"
            assert res.verdict == SafetyVerdict.BLOCK

    def test_blocks_secret_exfiltration(self):
        gate = DeterministicSafetyGate()
        exfil_attempts = [
            "cat .env",
            "print api_key",
            "dump all aws_secret keys",
        ]
        for attempt in exfil_attempts:
            res = gate.check(attempt)
            assert not res.passed, f"Should have blocked: {attempt}"
            assert res.verdict == SafetyVerdict.BLOCK

    def test_allows_benign_actions(self):
        gate = DeterministicSafetyGate()
        benign_actions = [
            "SELECT id, name FROM users WHERE active = 1;",
            "git status",
            "pytest tests/test_memory.py",
            "def calculate_total(items): return sum(items)",
        ]
        for action in benign_actions:
            res = gate.check(action)
            assert res.passed
            assert res.verdict == SafetyVerdict.ALLOW

    def test_custom_rule_enforcement(self):
        def no_network_rule(action, context):
            if "curl " in action or "wget " in action:
                return "Network requests prohibited in sandbox"
            return None

        gate = DeterministicSafetyGate(custom_rules=[no_network_rule])
        assert not gate.check("curl http://example.com").passed
        assert gate.check("echo hello").passed


# ======================================================================
# 2. Beta Posterior Memory Backend
# ======================================================================

class TestMemoryBetaDecisionBackend:
    """Verify statistical decision modeling conditioned on CogniCore memory."""

    def test_sparse_data_triggers_escalation(self):
        mem = TFIDFMemoryBackend()
        backend = MemoryBetaDecisionBackend(memory=mem, min_observations=3.0)

        # Cold start (0 observations)
        res = backend.score("Use binary search algorithm", context={"category": "algo"})

        # Prior is Beta(1, 1) -> mean 0.5, but effective_n is 0 -> MUST escalate
        assert res.escalate is True
        assert res.uncertainty > 0.40
        assert "Insufficient history" in res.reasoning

    def test_abundant_success_shrinks_uncertainty_and_avoids_escalation(self):
        mem = TFIDFMemoryBackend()
        # Seed 10 successful executions
        for i in range(10):
            mem.store(MemoryEntry(
                text=f"Applied indexing on user_id column attempt {i}",
                category="db_opt",
                correct=True,
            ))

        backend = MemoryBetaDecisionBackend(
            memory=mem, min_observations=3.0, max_uncertainty=0.40
        )
        res = backend.score("Apply indexing on user_id column", context={"category": "db_opt"})

        # High probability, narrow uncertainty -> fast decision, no escalation
        assert res.probability > 0.80
        assert res.uncertainty < 0.35
        assert res.escalate is False

    def test_choose_primitive(self):
        mem = TFIDFMemoryBackend()
        # Seed positive feedback for option A, negative for option B
        for i in range(5):
            mem.store(MemoryEntry(
                text=f"Used merge sort on list",
                category="sorting",
                correct=True,
            ))
        for i in range(5):
            mem.store(MemoryEntry(
                text=f"Used bubble sort on list",
                category="sorting",
                correct=False,
            ))

        backend = MemoryBetaDecisionBackend(memory=mem, min_observations=2.0)
        candidates = ["Used merge sort on list", "Used bubble sort on list"]
        res = backend.choose(candidates, context={"category": "sorting"})

        assert res.decision == "Used merge sort on list"
        assert res.probability > 0.65
        assert res.metadata["all_scores"][1]["candidate"] == "Used bubble sort on list"
        assert res.probability > res.metadata["all_scores"][1]["p"] + 0.30

    def test_yes_no_primitive(self):
        mem = TFIDFMemoryBackend()
        for i in range(6):
            mem.store(MemoryEntry(
                text="Validated JWT token signature with public key",
                category="auth",
                correct=True,
            ))

        backend = MemoryBetaDecisionBackend(memory=mem, min_observations=2.0)
        res = backend.yes_no(
            "Validate JWT token signature with public key",
            context={"category": "auth"},
        )
        assert res.decision is True
        assert res.probability > 0.75


# ======================================================================
# 3. Calibration Tooling
# ======================================================================

class TestCalibrationTooling:
    """Verify ECE, Brier score, and temperature scaling."""

    def test_perfectly_calibrated_ece_is_zero(self):
        # 10 samples: 5 predicted 0.0 (all 0), 5 predicted 1.0 (all 1)
        probs = [0.0] * 5 + [1.0] * 5
        outcomes = [False] * 5 + [True] * 5
        ece = expected_calibration_error(probs, outcomes, n_bins=5)
        assert ece == pytest.approx(0.0, abs=1e-4)

    def test_uncalibrated_ece_is_positive(self):
        # Overconfident: predicts 0.99 but outcomes are all False
        probs = [0.99] * 10
        outcomes = [False] * 10
        ece = expected_calibration_error(probs, outcomes, n_bins=5)
        assert ece > 0.90

    def test_brier_score_computation(self):
        probs = [0.8, 0.2]
        outcomes = [True, False]
        # (0.8 - 1)^2 = 0.04; (0.2 - 0)^2 = 0.04; mean = 0.04
        bs = brier_score(probs, outcomes)
        assert bs == pytest.approx(0.04, abs=1e-4)

    def test_reliability_diagram_structure(self):
        probs = [0.1, 0.4, 0.85]
        outcomes = [False, False, True]
        diag = compute_reliability_diagram(probs, outcomes, n_bins=5)
        assert "bins" in diag
        assert "ece" in diag
        assert "brier" in diag
        assert diag["total_samples"] == 3

    def test_temperature_scaler_reduces_overconfidence(self):
        scaler = TemperatureScaler(temperature=2.0)
        uncalibrated_p = 0.95
        calibrated_p = scaler.calibrate(uncalibrated_p)
        assert calibrated_p < uncalibrated_p  # Higher temperature softens extreme probabilities


# ======================================================================
# 4. Probabilistic Cognitive Control (PCCA Controller)
# ======================================================================

class TestProbabilisticCognitiveControl:
    """End-to-end integration of safety gate, decision model, and escalation."""

    def test_safety_gate_blocks_before_probability_model(self):
        mem = TFIDFMemoryBackend()
        backend = MemoryBetaDecisionBackend(memory=mem)
        pcca = ProbabilisticCognitiveControl(backend=backend)

        res = pcca.evaluate_action(
            action="rm -rf / --no-preserve-root",
            context={"category": "admin"},
        )
        assert res.decision is None
        assert res.probability == 0.0
        assert "BLOCKED by safety gate" in res.reasoning
        assert pcca.blocked_decisions == 1

    def test_escalation_to_slow_reasoner_when_uncertain(self):
        mem = TFIDFMemoryBackend()
        backend = MemoryBetaDecisionBackend(memory=mem, min_observations=5.0)
        pcca = ProbabilisticCognitiveControl(backend=backend)

        slow_reasoner_called = False
        def slow_reasoner_stub(action, context):
            nonlocal slow_reasoner_called
            slow_reasoner_called = True
            return "Slow LLM Reasoner: approved with caution"

        res = pcca.evaluate_action(
            action="Refactor core dispatch pipeline",
            context={"category": "refactor"},
            slow_reasoner=slow_reasoner_stub,
        )

        assert slow_reasoner_called is True
        assert pcca.escalated_decisions == 1
        assert "Escalated to slow reasoner" in res.reasoning

    def test_fast_decision_without_slow_reasoner_when_confident(self):
        mem = TFIDFMemoryBackend()
        for i in range(12):
            mem.store(MemoryEntry(
                text="Applied retry with exponential backoff on network socket",
                category="net",
                correct=True,
            ))

        backend = MemoryBetaDecisionBackend(memory=mem, min_observations=3.0)
        pcca = ProbabilisticCognitiveControl(backend=backend)

        slow_reasoner_called = False
        def slow_reasoner_stub(action, context):
            nonlocal slow_reasoner_called
            slow_reasoner_called = True
            return "Should not be called"

        res = pcca.evaluate_action(
            action="Apply retry with exponential backoff on network socket",
            context={"category": "net"},
            slow_reasoner=slow_reasoner_stub,
        )

        assert slow_reasoner_called is False
        assert pcca.fast_decisions == 1
        assert res.probability > 0.80

    def test_metrics_and_calibration_tracking(self):
        pcca = ProbabilisticCognitiveControl()
        pcca.evaluate_action("test action 1", context={}, task_id="t1")
        pcca.record_outcome("t1", True)
        pcca.evaluate_action("test action 2", context={}, task_id="t2")
        pcca.record_outcome("t2", False)

        metrics = pcca.get_calibration_metrics()
        assert metrics["samples"] == 2
        assert "ece" in metrics
        assert "brier" in metrics

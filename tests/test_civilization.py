import unittest
import json
import time
import urllib.request
import threading
from cognicore.civilization import Civilization, CivilizationServer, InsightReport

class DummyEvent:
    def __init__(self, action, reward):
        self.output_text = action
        self.reward = reward

class DummyStore:
    def __init__(self, events):
        self.events = events
    def get_all(self):
        return self.events

class DummyAgent:
    def __init__(self):
        self.store = DummyStore([
            DummyEvent("move_up", 1.0),
            DummyEvent("move_down", -1.0),
            DummyEvent("recall_memory", 0.0)
        ])
        self.civilization_priors = {}

class TestCivilization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Let the OS allocate an available port to avoid collisions in CI.
        cls.server = CivilizationServer(port=0)
        cls.server.start()
        cls.port = cls.server.server.server_port
        time.sleep(0.5) # Wait for server to start

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        # Clear server state between tests
        self.server.server.RequestHandlerClass.server_state = {
            "peers": set(),
            "contributions": {}
        }
        self.agent = DummyAgent()
        self.civ = Civilization(self.agent)
        self.address = f"http://localhost:{self.port}"

    def test_insight_report_init(self):
        # 1. Test InsightReport initializes properly
        report = InsightReport({"average_episode_length": 5.0})
        self.assertEqual(report.average_episode_length, 5.0)

    def test_insight_report_to_dict(self):
        # 2. Test InsightReport conversion to dict
        report = InsightReport({"contributing_agent_count": 2})
        d = report.to_dict()
        self.assertEqual(d["contributing_agent_count"], 2)

    def test_join_federated(self):
        # 3. Test joining civilization successfully
        success = self.civ.join(self.address, "federated")
        self.assertTrue(success)
        self.assertIsNotNone(self.civ.peer_id)
        self.assertEqual(self.civ.privacy_level, "federated")

    def test_join_cognicore_scheme(self):
        # 4. Test joining with cognicore:// scheme rewriting
        success = self.civ.join(f"cognicore://localhost:{self.port}", "federated")
        self.assertTrue(success)
        self.assertIsNotNone(self.civ.peer_id)

    def test_join_connection_failure(self):
        # 5. Test joining invalid address handles exception gracefully
        civ2 = Civilization(self.agent)
        success = civ2.join("http://localhost:99999", "federated") # Invalid port
        self.assertFalse(success)
        self.assertIsNone(civ2.peer_id)

    def test_extract_statistics(self):
        # 6. Test statistics extraction correctly omits raw obs and computes stats
        stats = self.civ.extract_statistics()
        self.assertIn("action_distribution", stats)
        self.assertEqual(stats["episode_length"], 3)
        self.assertEqual(stats["action_distribution"]["move_up"], 1)

    def test_extract_statistics_failure_modes(self):
        # 7. Test extraction correctly identifies failure modes
        stats = self.civ.extract_statistics()
        # move_up was before move_down (-1.0)
        self.assertEqual(stats["failure_frequencies"].get("move_up"), 1)

    def test_extract_statistics_memory_rate(self):
        # 8. Test memory access rate calculation
        stats = self.civ.extract_statistics()
        # 1 out of 3 actions is recall
        self.assertAlmostEqual(stats["memory_access_rate"], 1/3)

    def test_contribute_isolated(self):
        # 9. Test contribution is blocked if privacy is isolated
        self.civ.join(self.address, "isolated")
        success = self.civ.contribute("env_test")
        self.assertFalse(success)

    def test_contribute_federated(self):
        # 10. Test successful contribution
        self.civ.join(self.address, "federated")
        success = self.civ.contribute("env_test")
        self.assertTrue(success)

    def test_contribute_unauthorized(self):
        # 11. Test contribution rejected if peer_id is invalid
        self.civ.address = self.address
        self.civ.privacy_level = "federated"
        self.civ.peer_id = "invalid-id"
        success = self.civ.contribute("env_test")
        self.assertFalse(success)

    def test_bft_rejects_outlier(self):
        # 12. Test Byzantine Fault Tolerance rejects absurd episode lengths
        self.civ.join(self.address, "federated")
        # Manipulate agent to have huge history
        self.agent.store.events = [DummyEvent("a", 1.0)] * 150000
        success = self.civ.contribute("env_test")
        self.assertFalse(success) # Rejected by BFT

    def test_global_insights_empty(self):
        # 13. Test fetching insights for unknown env
        self.civ.join(self.address, "federated")
        report = self.civ.global_insights("unknown_env")
        self.assertEqual(report.contributing_agent_count, 0)

    def test_global_insights_aggregated(self):
        # 14. Test fetching and aggregating insights from multiple agents
        self.civ.join(self.address, "federated")
        self.civ.contribute("env_test")
        
        agent2 = DummyAgent()
        civ2 = Civilization(agent2)
        civ2.join(self.address, "federated")
        civ2.contribute("env_test")
        
        report = self.civ.global_insights("env_test")
        self.assertEqual(report.contributing_agent_count, 2)
        self.assertEqual(report.average_episode_length, 3.0)
        self.assertEqual(report.top_failure_modes.get("move_up"), 2)

    def test_absorb_insights(self):
        # 15. Test absorbing insights into agent priors
        report = InsightReport({
            "most_effective_strategies": {"move_up": 10.0}
        })
        self.civ.absorb(report)
        self.assertEqual(self.agent.civilization_priors["most_effective_strategies"]["move_up"], 10.0)


if __name__ == "__main__":
    unittest.main()

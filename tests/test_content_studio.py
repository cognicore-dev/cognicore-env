"""
Content Studio Tests — Unit, Integration, and E2E.

All tests run in mock mode (no API keys needed).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

# Force mock mode for all tests
os.environ["MOCK_MODE"] = "true"


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def config():
    """Create a test config with mock mode enabled."""
    from content_studio.config import StudioConfig
    return StudioConfig(
        MOCK_MODE=True,
        DB_PATH=str(Path(tempfile.mkdtemp()) / "test_studio.db"),
        OUTPUT_DIR=str(Path(tempfile.mkdtemp()) / "test_output"),
    )


@pytest.fixture
def project():
    """Create a test project."""
    from content_studio.models import Project
    return Project(
        name="Test Project",
        figma_input="A modern SaaS dashboard with clean minimalist design",
        language="en",
        output_type="pptx",
    )


# ======================================================================
# Unit Tests — Config
# ======================================================================

class TestConfig:
    """Tests for configuration loading."""

    def test_config_defaults(self, config):
        assert config.MOCK_MODE is True
        assert config.LLM_PROVIDER == "groq"

    def test_config_connector_status(self, config):
        status = config.connector_status()
        assert "figma" in status
        assert "llm" in status
        assert "elevenlabs" in status
        assert "sarvam" in status
        assert "canva" in status
        assert "cognicore" in status
        # All should be mock in test mode
        assert status["figma"] == "mock"
        assert "live" in status["cognicore"]

    def test_config_load_from_env(self):
        from content_studio.config import load_config
        os.environ["MOCK_MODE"] = "true"
        cfg = load_config()
        assert cfg.MOCK_MODE is True

    def test_supported_languages(self):
        from content_studio.config import SUPPORTED_LANGUAGES
        assert "en" in SUPPORTED_LANGUAGES
        assert "hi" in SUPPORTED_LANGUAGES
        assert "ta" in SUPPORTED_LANGUAGES


# ======================================================================
# Unit Tests — Models
# ======================================================================

class TestModels:
    """Tests for data models."""

    def test_scene_creation(self):
        from content_studio.models import Scene
        scene = Scene(title="Test", narration_text="Hello", duration_sec=5.0)
        assert scene.scene_id.startswith("scene_")
        assert scene.title == "Test"
        d = scene.to_dict()
        assert d["title"] == "Test"

    def test_scene_from_dict(self):
        from content_studio.models import Scene
        d = {"title": "Test", "narration_text": "Hello", "duration_sec": 8.0}
        scene = Scene.from_dict(d)
        assert scene.title == "Test"
        assert scene.duration_sec == 8.0

    def test_project_creation(self, project):
        assert project.project_id.startswith("proj_")
        assert project.state.value == "pending"
        assert project.name == "Test Project"

    def test_project_to_dict(self, project):
        d = project.to_dict()
        assert d["state"] == "pending"
        assert d["name"] == "Test Project"

    def test_timeline_creation(self):
        from content_studio.models import Scene, Timeline
        scenes = [Scene(title="S1", duration_sec=5.0), Scene(title="S2", duration_sec=10.0)]
        tl = Timeline(scenes=scenes, total_duration_sec=15.0, sync_status="synced")
        d = tl.to_dict()
        assert len(d["scenes"]) == 2
        assert d["total_duration_sec"] == 15.0


# ======================================================================
# Unit Tests — Workflow
# ======================================================================

class TestWorkflow:
    """Tests for workflow state machine."""

    def test_valid_transitions(self, project):
        from content_studio.workflow import transition
        from content_studio.models import WorkflowState
        transition(project, WorkflowState.RUNNING)
        assert project.state == WorkflowState.RUNNING
        transition(project, WorkflowState.COMPLETED)
        assert project.state == WorkflowState.COMPLETED

    def test_invalid_transition_raises(self, project):
        from content_studio.workflow import transition, WorkflowError
        from content_studio.models import WorkflowState
        with pytest.raises(WorkflowError):
            transition(project, WorkflowState.COMPLETED)  # can't go pending→completed

    def test_failed_transition(self, project):
        from content_studio.workflow import transition
        from content_studio.models import WorkflowState
        transition(project, WorkflowState.RUNNING)
        transition(project, WorkflowState.FAILED, "Something broke")
        assert project.state == WorkflowState.FAILED
        assert project.error == "Something broke"

    def test_retry_from_failed(self, project):
        from content_studio.workflow import transition
        from content_studio.models import WorkflowState
        transition(project, WorkflowState.RUNNING)
        transition(project, WorkflowState.FAILED, "error")
        transition(project, WorkflowState.RUNNING)  # retry
        assert project.state == WorkflowState.RUNNING

    def test_record_step(self, project):
        from content_studio.workflow import record_step
        sr = record_step(project, "test_step", "success", is_mock=True, duration_ms=100)
        assert sr.step_name == "test_step"
        assert sr.is_mock is True
        assert len(project.step_results) == 1

    def test_should_flag_for_review(self, project):
        from content_studio.workflow import record_step, should_flag_for_review
        record_step(project, "step1", "mock", is_mock=True)
        assert should_flag_for_review(project) is True


# ======================================================================
# Unit Tests — Connectors (Mock Mode)
# ======================================================================

class TestConnectorsMock:
    """Tests for all connectors in mock mode."""

    def test_figma_mock(self, config):
        from content_studio.connectors.figma_connector import FigmaConnector
        conn = FigmaConnector(config)
        assert conn.is_mock is True
        result = conn.extract_design("https://figma.com/file/abc123/test", config)
        assert result["is_mock"] is True
        assert result["concept"] == "Minimalist"
        assert "Inter" in result["fonts"]

    def test_llm_mock(self, config):
        from content_studio.connectors.llm_connector import LLMConnector
        conn = LLMConnector(config)
        assert conn.is_mock is True
        script, scenes = conn.generate_script({"concept": "Minimalist"}, "pptx")
        assert len(scenes) == 3
        assert "[MOCK]" in script
        assert scenes[0].title == "Platform Overview & Architecture"

    def test_sarvam_mock(self, config):
        from content_studio.connectors.sarvam_connector import SarvamConnector
        conn = SarvamConnector(config)
        assert conn.is_mock is True
        result = conn.translate("Hello world", "hi")
        assert result["is_mock"] is True
        assert "MOCK" in result["translated_text"]
        assert "Hindi" in result["translated_text"]

    def test_canva_mock(self, config):
        from content_studio.connectors.canva_connector import CanvaConnector
        conn = CanvaConnector(config)
        assert conn.is_mock is True  # Always mock

    def test_elevenlabs_mock(self, config):
        from content_studio.connectors.elevenlabs_connector import ElevenLabsConnector
        conn = ElevenLabsConnector(config)
        assert conn.is_mock is True
        result = conn.generate_narration("Hello world, this is a test narration.")
        assert result["is_mock"] is True
        assert result["audio_bytes"] is None
        assert result["duration_sec"] > 0

    def test_connector_base_label(self, config):
        from content_studio.connectors.base import ConnectorBase
        conn = ConnectorBase(config)
        assert conn._label("test") == "[MOCK] test"


# ======================================================================
# Unit Tests — Services
# ======================================================================

class TestServices:
    """Tests for service modules."""

    def test_timeline_sync_all_audio(self):
        from content_studio.models import Scene
        from content_studio.services.timeline_sync import TimelineSync
        scenes = [
            Scene(title="S1", duration_sec=5.0, audio_duration_sec=7.0),
            Scene(title="S2", duration_sec=5.0, audio_duration_sec=10.0),
        ]
        ts = TimelineSync()
        timeline = ts.synchronize(scenes)
        assert timeline.sync_status == "synced"
        assert timeline.total_duration_sec == 17.0
        assert scenes[0].duration_sec == 7.0

    def test_timeline_sync_partial(self):
        from content_studio.models import Scene
        from content_studio.services.timeline_sync import TimelineSync
        scenes = [
            Scene(title="S1", duration_sec=5.0, audio_duration_sec=7.0),
            Scene(title="S2", duration_sec=5.0),  # no audio
        ]
        ts = TimelineSync()
        timeline = ts.synchronize(scenes)
        assert timeline.sync_status == "partial"

    def test_timeline_sync_none(self):
        from content_studio.models import Scene
        from content_studio.services.timeline_sync import TimelineSync
        scenes = [Scene(title="S1", duration_sec=5.0)]
        ts = TimelineSync()
        timeline = ts.synchronize(scenes)
        assert timeline.sync_status == "unsynced"


# ======================================================================
# Unit Tests — Conversation Compressor
# ======================================================================

class TestConversationCompressor:
    """Tests for conversation compression."""

    def test_compress_reduces_tokens(self):
        from content_studio.memory.conversation_compressor import ConversationCompressor
        comp = ConversationCompressor()
        conv = [
            {"role": "user", "content": "Please could you help me generate a presentation"},
            {"role": "agent", "content": "Sure, I would like to help you with that"},
            {"role": "system", "content": "Thank you, the task is complete"},
        ]
        result = comp.compress(conv)
        assert result["compressed_tokens"] <= result["original_tokens"]
        assert result["reduction_pct"] >= 0
        assert "U:" in result["compressed_text"]

    def test_estimate_tokens(self):
        from content_studio.memory.conversation_compressor import ConversationCompressor
        comp = ConversationCompressor()
        tokens = comp.estimate_tokens("Hello world this is a test")
        assert tokens > 0
        assert isinstance(tokens, int)


# ======================================================================
# Integration Test — Full Pipeline Mock Mode
# ======================================================================

class TestFullPipelineMock:
    """Integration test running the full pipeline in mock mode."""

    def test_full_pipeline_mock(self, config, project):
        """Run the complete pipeline in mock mode — no API keys needed."""
        from content_studio.orchestrator import PipelineOrchestrator
        from content_studio.models import WorkflowState

        orch = PipelineOrchestrator(config)
        result = orch.run(project)

        # Pipeline should complete (or needs_review since mock)
        assert result.state in (
            WorkflowState.COMPLETED,
            WorkflowState.NEEDS_REVIEW,
        ), f"Unexpected state: {result.state.value}, error: {result.error}"

        # Should have generated scenes
        assert len(result.scenes) > 0

        # Should have a script
        assert result.script is not None
        assert len(result.script) > 0

        # Should have a timeline
        assert result.timeline is not None
        assert result.timeline.total_duration_sec > 0

        # Should have step results
        assert len(result.step_results) >= 5  # at least figma, script, visual, voice, timeline

        # All steps should be mock or success
        for sr in result.step_results:
            assert sr.status in ("success", "mock", "skipped"), (
                f"Step {sr.step_name} has unexpected status: {sr.status}"
            )

    def test_pipeline_project_to_dict(self, config, project):
        """Verify the full project serializes to dict after pipeline run."""
        from content_studio.orchestrator import PipelineOrchestrator

        orch = PipelineOrchestrator(config)
        result = orch.run(project)
        d = result.to_dict()

        assert "project_id" in d
        assert "state" in d
        assert "scenes" in d
        assert isinstance(d["scenes"], list)


# ======================================================================
# E2E Test — Deterministic Demo Flow
# ======================================================================

class TestE2EDemoFlow:
    """End-to-end test simulating the complete demo flow with fixtures."""

    def test_e2e_demo(self, config):
        """Simulate full user flow: create project → run pipeline → check outputs."""
        from content_studio.models import Project, WorkflowState
        from content_studio.orchestrator import PipelineOrchestrator

        # Step 1: Create project (simulating API call)
        project = Project(
            name="E2E Demo Project",
            figma_input="A fintech dashboard with dark theme, green accents, and Roboto font",
            language="en",
            output_type="pptx",
        )
        assert project.state == WorkflowState.PENDING

        # Step 2: Run pipeline
        orch = PipelineOrchestrator(config)
        result = orch.run(project)

        # Step 3: Verify outputs
        assert result.state in (WorkflowState.COMPLETED, WorkflowState.NEEDS_REVIEW)
        assert result.script is not None
        assert len(result.scenes) == 3
        assert result.timeline is not None

        # Step 4: Verify serialization
        d = result.to_dict()
        assert d["name"] == "E2E Demo Project"
        assert len(d["step_results"]) > 0

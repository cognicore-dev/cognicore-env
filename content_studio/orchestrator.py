"""
Content Studio Pipeline Orchestrator.

Coordinates the full AI content creation workflow:
Figma → LLM Script → Localization → Visuals → Voice → Timeline → Export → CogniCore
"""

import logging
import time
import traceback
from pathlib import Path
from typing import Optional

from content_studio.config import StudioConfig
from content_studio.models import Project, Scene, Timeline, WorkflowState, StepResult
from content_studio.workflow import transition, record_step, should_flag_for_review

logger = logging.getLogger("content_studio.orchestrator")


class PipelineOrchestrator:
    """Orchestrates the full AI Content Studio pipeline.

    Initializes all connectors, services, and the CogniCore memory layer,
    then runs the pipeline steps in sequence with error handling and
    step recording.
    """

    def __init__(self, config: StudioConfig) -> None:
        self.config = config

        # --- Memory layer ---
        try:
            from content_studio.memory.cognicore_layer import CogniCoreLayer
            from content_studio.memory.conversation_compressor import ConversationCompressor
            self.memory = CogniCoreLayer(config)
            self.compressor = ConversationCompressor()
        except Exception as e:
            logger.warning(f"CogniCore memory layer init failed: {e}")
            self.memory = None
            self.compressor = None

        # --- Replay & Immune Layer ---
        try:
            from cognicore.replay.store import EventStore
            from cognicore.replay.recorder import EventRecorder
            from cognicore.immune import NexusShield
            db_dir = Path(config.DB_PATH).parent
            self.event_store = EventStore(str(db_dir / "studio_events.db"))
            self.recorder = EventRecorder(store=self.event_store)
            self.shield = NexusShield()
        except Exception as e:
            logger.warning(f"Replay/Shield init failed: {e}")
            self.event_store = None
            self.recorder = None
            self.shield = None

        # --- Connectors ---
        from content_studio.connectors.figma_connector import FigmaConnector
        from content_studio.connectors.llm_connector import LLMConnector
        from content_studio.connectors.sarvam_connector import SarvamConnector
        from content_studio.connectors.canva_connector import CanvaConnector
        from content_studio.connectors.elevenlabs_connector import ElevenLabsConnector

        self.figma = FigmaConnector(config)
        self.llm = LLMConnector(config)
        self.sarvam = SarvamConnector(config)
        self.canva = CanvaConnector(config)
        self.elevenlabs = ElevenLabsConnector(config)

        # --- Services (lazy, use connectors) ---
        from content_studio.services.timeline_sync import TimelineSync
        from content_studio.services.export import ExportService

        self.timeline_sync = TimelineSync()
        self.export_svc = ExportService()

        logger.info("PipelineOrchestrator initialized.")

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def run(self, project: Project) -> Project:
        """Execute the full content creation pipeline.

        Steps:
        1. Retrieve prior CogniCore experiences
        2. Extract design context (Figma)
        3. Generate script & scenes (LLM)
        4. Localize (Sarvam AI) — if language ≠ 'en'
        5. Generate visuals (Canva / python-pptx)
        6. Generate voice narration (ElevenLabs)
        7. Synchronize timeline
        8. Export final output
        9. Record experience in CogniCore
        """
        try:
            transition(project, WorkflowState.RUNNING)
            out_dir = Path(self.config.OUTPUT_DIR) / project.project_id
            out_dir.mkdir(parents=True, exist_ok=True)

            # ── Step 0: Retrieve prior experiences ────────────────────
            self._step_retrieve_experiences(project)

            # ── Step 1: Figma extract ─────────────────────────────────
            design_context = self._step_figma(project)

            # ── Step 2: Script generation ─────────────────────────────
            self._step_script(project, design_context)

            # ── Step 3: Localization ──────────────────────────────────
            if project.language != "en":
                self._step_localize(project)

            # ── Step 4: Visual generation ─────────────────────────────
            self._step_visuals(project, design_context, str(out_dir))

            # ── Step 5: Voice generation ──────────────────────────────
            self._step_voice(project, str(out_dir))

            # ── Step 6: Timeline sync ─────────────────────────────────
            self._step_timeline(project)

            # ── Step 7: Export ────────────────────────────────────────
            self._step_export(project, str(out_dir))

            # ── Step 8: CogniCore record ──────────────────────────────
            self._step_cognicore(project)

            # ── Final state ───────────────────────────────────────────
            if should_flag_for_review(project):
                transition(project, WorkflowState.NEEDS_REVIEW, "Mock adapters used or partial sync")
            else:
                transition(project, WorkflowState.COMPLETED)

        except Exception as e:
            logger.error(f"Pipeline failed: {e}\n{traceback.format_exc()}")
            if self.recorder:
                self.recorder.record_simple(project.project_id, "task_failed", step=7, output_text=str(e))
            try:
                transition(project, WorkflowState.FAILED, str(e))
            except Exception:
                project.state = WorkflowState.FAILED
                project.error = str(e)

        return project

    # ------------------------------------------------------------------
    # Individual pipeline steps
    # ------------------------------------------------------------------

    def _step_retrieve_experiences(self, project: Project) -> None:
        """Retrieve prior CogniCore experiences for context."""
        if not self.memory:
            return
        try:
            exps = self.memory.retrieve_experiences(
                f"content_studio {project.output_type} {project.figma_input[:50]}"
            )
            project.prior_experiences = exps
            if exps:
                logger.info(f"Retrieved {len(exps)} prior experiences")
            if self.recorder:
                self.recorder.record_simple(project.project_id, "memory_retrieved", step=2, output_text=f"Retrieved {len(exps)} prior experiences from CogniCore")
        except Exception as e:
            logger.warning(f"Experience retrieval failed: {e}")

    def _step_figma(self, project: Project) -> dict:
        """Extract design context from Figma input."""
        t0 = time.time()
        try:
            # Shield inspection
            if self.shield:
                dec = self.shield(project.figma_input)
                if dec.blocked:
                    if self.recorder:
                        self.recorder.record_simple(project.project_id, "immune_blocked", step=1, output_text=f"NexusShield Blocked Threat: {dec.reason} (score {dec.threat_score})")
                    raise ValueError(f"NexusShield Blocked Threat: {dec.reason} (score {dec.threat_score})")

            if self.recorder:
                self.recorder.record_simple(project.project_id, "task_start", step=1, input_text=project.figma_input)

            design_context = self.figma.extract_design(project.figma_input, self.config)
            project.design_context = design_context
            is_mock = design_context.get("is_mock", self.figma.is_mock)
            record_step(project, "figma_extract", "mock" if is_mock else "success",
                        is_mock=is_mock, duration_ms=(time.time() - t0) * 1000)
            self._log_conversation(project, "system",
                                   f"Figma design extracted: {design_context.get('concept', 'unknown')}")
            return design_context
        except Exception as e:
            record_step(project, "figma_extract", "failed", error=str(e),
                        duration_ms=(time.time() - t0) * 1000)
            raise

    def _step_script(self, project: Project, design_context: dict) -> None:
        """Generate script and scenes via LLM."""
        t0 = time.time()
        try:
            script_text, scenes = self.llm.generate_script(
                design_context, project.output_type
            )
            project.script = script_text
            project.scenes = scenes
            is_mock = self.llm.is_mock
            record_step(project, "script_gen", "mock" if is_mock else "success",
                        is_mock=is_mock, duration_ms=(time.time() - t0) * 1000)
            self._log_conversation(project, "assistant",
                                   f"Generated script with {len(scenes)} scenes")
            if self.recorder:
                self.recorder.record_simple(project.project_id, "plan_generated", step=3, output_text=f"Generated script with {len(scenes)} scenes")
        except Exception as e:
            record_step(project, "script_gen", "failed", error=str(e),
                        duration_ms=(time.time() - t0) * 1000)
            raise

    def _step_localize(self, project: Project) -> None:
        """Translate scenes using Sarvam AI."""
        t0 = time.time()
        try:
            for scene in project.scenes:
                result = self.sarvam.translate(scene.narration_text, project.language)
                scene.narration_text = result.get("translated_text", scene.narration_text)
            is_mock = self.sarvam.is_mock
            record_step(project, "localize", "mock" if is_mock else "success",
                        is_mock=is_mock, duration_ms=(time.time() - t0) * 1000)
            self._log_conversation(project, "system",
                                   f"Localized {len(project.scenes)} scenes to {project.language}")
        except Exception as e:
            record_step(project, "localize", "failed", error=str(e),
                        duration_ms=(time.time() - t0) * 1000)
            raise

    def _step_visuals(self, project: Project, design_context: dict, out_dir: str) -> None:
        """Generate visual presentation or video plan."""
        t0 = time.time()
        try:
            from content_studio.services.visual_gen import VisualGenerator
            vg = VisualGenerator()
            result = vg.generate(
                project.scenes, design_context, project.output_type, out_dir, self.config
            )
            is_mock = result.get("is_mock", True)
            if result.get("file_path"):
                project.artifacts["presentation"] = result["file_path"]

            # Trigger Canva design creation
            try:
                if project.output_type.lower() == "video":
                    canva_res = self.canva.create_video(project.scenes, design_context)
                else:
                    canva_res = self.canva.create_presentation(project.scenes, design_context)

                if canva_res.get("edit_url"):
                    project.artifacts["canva_edit_url"] = canva_res["edit_url"]
                if canva_res.get("view_url"):
                    project.artifacts["canva_view_url"] = canva_res["view_url"]
                if not canva_res.get("is_mock", True):
                    is_mock = False
            except Exception as e:
                logger.warning(f"Canva design creation skipped: {e}")

            record_step(project, "visual_gen", "mock" if is_mock else "success",
                        is_mock=is_mock, duration_ms=(time.time() - t0) * 1000)
        except Exception as e:
            record_step(project, "visual_gen", "failed", error=str(e),
                        duration_ms=(time.time() - t0) * 1000)
            raise

    def _step_voice(self, project: Project, out_dir: str) -> None:
        """Generate narration audio for each scene."""
        t0 = time.time()
        try:
            audio_files = []
            is_mock = self.elevenlabs.is_mock
            for scene in project.scenes:
                result = self.elevenlabs.generate_narration(
                    text=scene.narration_text,
                    voice_name="rachel",
                )
                duration = result.get("duration_sec", 5.0)
                scene.audio_duration_sec = duration

                # Save audio if we got real bytes
                audio_bytes = result.get("audio_bytes")
                if audio_bytes:
                    audio_path = str(Path(out_dir) / f"{scene.scene_id}.mp3")
                    Path(audio_path).write_bytes(audio_bytes)
                    scene.audio_path = audio_path
                    audio_files.append(audio_path)
                is_mock = is_mock or result.get("is_mock", True)

            record_step(project, "voice_gen", "mock" if is_mock else "success",
                        is_mock=is_mock, duration_ms=(time.time() - t0) * 1000)
            if audio_files:
                project.artifacts["audio_files"] = ",".join(audio_files)
        except Exception as e:
            record_step(project, "voice_gen", "failed", error=str(e),
                        duration_ms=(time.time() - t0) * 1000)
            raise

    def _step_timeline(self, project: Project) -> None:
        """Synchronize scene durations with audio."""
        t0 = time.time()
        try:
            timeline = self.timeline_sync.synchronize(project.scenes)
            project.timeline = timeline
            is_mock = timeline.sync_status == "unsynced"
            record_step(project, "timeline_sync", "mock" if is_mock else "success",
                        is_mock=is_mock, duration_ms=(time.time() - t0) * 1000)
        except Exception as e:
            record_step(project, "timeline_sync", "failed", error=str(e),
                        duration_ms=(time.time() - t0) * 1000)
            raise

    def _step_export(self, project: Project, out_dir: str) -> None:
        """Export final PPTX or video plan."""
        t0 = time.time()
        try:
            if project.output_type.lower() == "pptx":
                path = self.export_svc.export_pptx(project, out_dir)
                project.artifacts["presentation.pptx"] = path

                # If Canva is connected, import the PPTX into Canva so all slides and text are populated!
                try:
                    if not self.canva.is_mock:
                        logger.info("Importing generated PPTX into Canva via /v1/imports...")
                        canva_import = self.canva.import_pptx_to_canva(path, project.name)
                        if canva_import.get("edit_url"):
                            project.artifacts["canva_edit_url"] = canva_import["edit_url"]
                        if canva_import.get("view_url"):
                            project.artifacts["canva_view_url"] = canva_import["view_url"]
                except Exception as ce:
                    logger.warning(f"Canva PPTX import failed: {ce}")

            else:
                path = self.export_svc.export_video_mp4(project, out_dir)
                project.artifacts["presentation.mp4"] = path
            project.artifacts["final_output"] = path
            record_step(project, "export", "success",
                        duration_ms=(time.time() - t0) * 1000)
            if self.recorder:
                self.recorder.record_simple(project.project_id, "task_solved", step=6, output_text=f"Exported {project.output_type.upper()}: {Path(path).name}")
        except Exception as e:
            record_step(project, "export", "failed", error=str(e),
                        duration_ms=(time.time() - t0) * 1000)
            raise

    def _step_cognicore(self, project: Project) -> None:
        """Record experience and compress conversation in CogniCore."""
        if not self.memory:
            record_step(project, "cognicore_record", "skipped")
            return
        t0 = time.time()
        try:
            self.memory.record_experience(project)
            # Compress conversation
            if self.compressor:
                conv = self.memory.get_conversation(project.project_id)
                conv_dicts = [
                    {"role": (e.metadata or {}).get("role", ""), "content": e.text}
                    for e in conv
                ]
                if conv_dicts:
                    compressed = self.compressor.compress(conv_dicts)
                    self.compressor.store_compressed(
                        project.project_id, compressed, self.memory
                    )
                    project.metadata["compression"] = {
                        "original_tokens": compressed["original_tokens"],
                        "compressed_tokens": compressed["compressed_tokens"],
                        "reduction_pct": compressed["reduction_pct"],
                    }
            record_step(project, "cognicore_record", "success",
                        duration_ms=(time.time() - t0) * 1000)
        except Exception as e:
            record_step(project, "cognicore_record", "failed", error=str(e),
                        duration_ms=(time.time() - t0) * 1000)
            # Don't raise — CogniCore failure shouldn't fail the pipeline

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log_conversation(self, project: Project, role: str, content: str) -> None:
        """Store a conversation turn in CogniCore if available."""
        if self.memory:
            try:
                self.memory.store_conversation(project.project_id, role, content)
            except Exception:
                pass

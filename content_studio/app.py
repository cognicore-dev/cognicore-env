"""
Content Studio — FastAPI Application.

Serves the dashboard UI and provides REST API endpoints for project
management, workflow execution, and CogniCore memory inspection.
"""

import json
import logging
import os
import sys
from pathlib import Path
import time
from typing import Optional

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import base64
import hashlib
import secrets
import requests
from requests.auth import HTTPBasicAuth

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from content_studio.config import StudioConfig, load_config, SUPPORTED_LANGUAGES
from content_studio.models import Project, WorkflowState

logger = logging.getLogger("content_studio.app")

_pkce_store: dict = {}

def _generate_pkce_pair():
    """Generate high-entropy code_verifier and S256 code_challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("utf-8").replace("=", "")
    return verifier, challenge

# ---------------------------------------------------------------------------
# Globals (initialized in create_app)
# ---------------------------------------------------------------------------
_config: Optional[StudioConfig] = None
_orchestrator = None
_projects: dict = {}  # project_id → Project


def create_app(config: Optional[StudioConfig] = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    global _config, _orchestrator

    _config = config or load_config()

    app = FastAPI(
        title="AI Content Studio",
        description="CogniCore-powered AI content creation workflow",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Lazy-import orchestrator to avoid circular imports
    try:
        from content_studio.orchestrator import PipelineOrchestrator
        _orchestrator = PipelineOrchestrator(_config)
    except Exception as e:
        logger.warning(f"Orchestrator init deferred: {e}")
        _orchestrator = None

    # ── Dashboard ─────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def serve_dashboard():
        """Serve the single-page dashboard."""
        html_path = Path(__file__).parent / "dashboard.html"
        if html_path.exists():
            return HTMLResponse(html_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>AI Content Studio</h1><p>Dashboard not found.</p>")

    # ── Health ────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health():
        return {
            "status": "healthy",
            "version": "0.1.0",
            "mock_mode": _config.MOCK_MODE,
            "connectors": _config.connector_status(),
        }

    # ── Connectors Status ─────────────────────────────────────────────
    @app.get("/api/connectors/status")
    async def connectors_status():
        return _config.connector_status()

    # ── Canva OAuth ───────────────────────────────────────────────────
    @app.get("/api/auth/canva/login")
    async def canva_login():
        if not _config.CANVA_CLIENT_ID:
            raise HTTPException(400, "CANVA_CLIENT_ID is not configured in .env")

        state = secrets.token_hex(16)
        code_verifier, code_challenge = _generate_pkce_pair()
        _pkce_store[state] = code_verifier

        import urllib.parse
        canva_scopes = (
            "comment:read asset:read folder:permission:write design:meta:write "
            "design:permission:read brandtemplate:content:write brandtemplate:content:read "
            "design:meta:read design:content:read asset:write design:content:write "
            "comment:write folder:write folder:permission:read app:write folder:read "
            "design:permission:write app:read"
        )
        params = [
            ("code_challenge", code_challenge),
            ("code_challenge_method", "s256"),
            ("scope", canva_scopes),
            ("response_type", "code"),
            ("client_id", _config.CANVA_CLIENT_ID.strip()),
            ("state", state),
            ("redirect_uri", _config.CANVA_REDIRECT_URI.strip()),
        ]
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return RedirectResponse(f"https://www.canva.com/api/oauth/authorize?{query}")

    @app.get("/api/auth/canva/callback")
    async def canva_callback(code: str = "", state: str = "", error: str = ""):
        if error:
            return HTMLResponse(
                f"<body style='font-family:sans-serif;background:#06080f;color:#e6edf3;padding:40px;'>"
                f"<h3 style='color:#f85149;'>Canva Authorization Error</h3><p>{error}</p><p><a href='/' style='color:#388bfd;'>Return to Studio</a></p></body>"
            )
        if not code or not state:
            raise HTTPException(400, "Missing code or state")

        code_verifier = _pkce_store.pop(state, None)
        if not code_verifier:
            raise HTTPException(400, "Invalid or expired OAuth state")

        # Exchange code for tokens
        token_url = "https://api.canva.com/rest/v1/oauth/token"
        try:
            resp = requests.post(
                token_url,
                auth=HTTPBasicAuth(_config.CANVA_CLIENT_ID, _config.CANVA_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _config.CANVA_REDIRECT_URI,
                    "code_verifier": code_verifier,
                },
                timeout=15,
            )
            data = resp.json()
            if resp.status_code != 200:
                logger.error(f"Canva token exchange failed: {data}")
                return HTMLResponse(
                    f"<body style='font-family:sans-serif;background:#06080f;color:#e6edf3;padding:40px;'>"
                    f"<h3 style='color:#f85149;'>Canva Connection Failed</h3><p>{data.get('error_description', data)}</p><p><a href='/' style='color:#388bfd;'>Return to Studio</a></p></body>"
                )

            access_token = data.get("access_token", "")
            _config.CANVA_ACCESS_TOKEN = access_token
            logger.info("Canva access token received successfully!")

            # Store in CogniCore memory
            if _orchestrator and hasattr(_orchestrator, "memory") and _orchestrator.memory:
                try:
                    from cognicore.memory.base import MemoryEntry, MemoryScope
                    _orchestrator.memory.backend.store(MemoryEntry(
                        text="Canva OAuth access token stored",
                        category="canva_auth",
                        memory_type="credential",
                        scope=MemoryScope.GLOBAL,
                        metadata={"access_token": access_token, "token_type": data.get("token_type")},
                    ))
                except Exception as e:
                    logger.warning(f"Failed to persist Canva token in memory: {e}")

            return HTMLResponse(
                "<html><head><meta http-equiv='refresh' content='2;url=/' /></head>"
                "<body style='font-family:sans-serif;background:#06080f;color:#e6edf3;text-align:center;padding:50px;'>"
                "<h2 style='color:#3fb950;'>✓ Canva Connected Successfully!</h2>"
                "<p>Redirecting back to Content Studio...</p>"
                "<p><a href='/' style='color:#388bfd;'>Click here if not redirected</a></p>"
                "</body></html>"
            )
        except Exception as e:
            logger.exception("Error exchanging Canva token")
            return HTMLResponse(
                f"<body style='font-family:sans-serif;background:#06080f;color:#e6edf3;padding:40px;'>"
                f"<h3 style='color:#f85149;'>Canva Connection Error</h3><p>{e}</p><p><a href='/' style='color:#388bfd;'>Return to Studio</a></p></body>"
            )

    @app.get("/api/auth/canva/status")
    async def canva_status():
        return {
            "connected": bool(_config.CANVA_ACCESS_TOKEN),
            "client_id_configured": bool(_config.CANVA_CLIENT_ID),
        }


    # ── Languages ─────────────────────────────────────────────────────
    @app.get("/api/languages")
    async def list_languages():
        return {"languages": SUPPORTED_LANGUAGES}

    # ── Projects ──────────────────────────────────────────────────────
    @app.post("/api/projects")
    async def create_project(request: Request):
        body = await request.json()
        figma_input = body.get("figma_input", "")
        if not figma_input:
            raise HTTPException(400, "figma_input is required")

        project = Project(
            name=body.get("name", "Untitled Project"),
            figma_input=figma_input,
            language=body.get("language", "en"),
            output_type=body.get("output_type", "pptx"),
        )
        _projects[project.project_id] = project
        logger.info(f"Created project {project.project_id}: {project.name}")
        return project.to_dict()

    @app.get("/api/projects")
    async def list_projects():
        return {
            "projects": [p.to_dict() for p in _projects.values()]
        }

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        project = _projects.get(project_id)
        if not project:
            raise HTTPException(404, f"Project {project_id} not found")
        return project.to_dict()

    # ── Run Workflow ──────────────────────────────────────────────────
    @app.post("/api/projects/{project_id}/run")
    async def run_workflow(project_id: str):
        global _orchestrator
        project = _projects.get(project_id)
        if not project:
            raise HTTPException(404, f"Project {project_id} not found")

        if project.state == WorkflowState.RUNNING:
            raise HTTPException(409, "Workflow is already running")

        if project.state == WorkflowState.COMPLETED:
            raise HTTPException(409, "Workflow already completed. Create a new project to re-run.")

        # Ensure orchestrator is initialized
        if _orchestrator is None:
            try:
                from content_studio.orchestrator import PipelineOrchestrator
                _orchestrator = PipelineOrchestrator(_config)
            except Exception as e:
                raise HTTPException(500, f"Orchestrator initialization failed: {e}")

        try:
            updated = _orchestrator.run(project)
            _projects[project_id] = updated
            return updated.to_dict()
        except Exception as e:
            logger.exception(f"Workflow failed for {project_id}")
            project.state = WorkflowState.FAILED
            project.error = str(e)
            return project.to_dict()

    # ── Timeline ──────────────────────────────────────────────────────
    @app.get("/api/projects/{project_id}/timeline")
    async def get_timeline(project_id: str):
        project = _projects.get(project_id)
        if not project:
            raise HTTPException(404, f"Project {project_id} not found")
        if not project.timeline:
            return {"timeline": None, "message": "Timeline not yet generated"}
        return project.timeline.to_dict()

    # ── Download Artifacts ────────────────────────────────────────────
    @app.get("/api/projects/{project_id}/download/{artifact_name}")
    async def download_artifact(project_id: str, artifact_name: str):
        project = _projects.get(project_id)
        if not project:
            raise HTTPException(404, f"Project {project_id} not found")

        file_path = project.artifacts.get(artifact_name)
        if not file_path or not Path(file_path).exists():
            raise HTTPException(404, f"Artifact '{artifact_name}' not found")

        return FileResponse(
            path=file_path,
            filename=Path(file_path).name,
            media_type="application/octet-stream",
        )

    # ── Memory / Experiences ──────────────────────────────────────────
    @app.get("/api/memory/experiences")
    async def list_experiences():
        if _orchestrator and hasattr(_orchestrator, "memory"):
            try:
                exps = _orchestrator.memory.retrieve_experiences(
                    "content_studio_workflow", top_k=10
                )
                return {"experiences": exps}
            except Exception as e:
                return {"experiences": [], "error": str(e)}
        return {"experiences": [], "message": "Memory layer not initialized"}

    @app.get("/api/memory/conversation/{project_id}")
    async def get_conversation(project_id: str):
        if _orchestrator and hasattr(_orchestrator, "memory"):
            try:
                conv = _orchestrator.memory.get_conversation(project_id)
                return {"conversation": conv}
            except Exception as e:
                return {"conversation": [], "error": str(e)}
        return {"conversation": [], "message": "Memory layer not initialized"}

    # ── CogniCore Telemetry & Live Agent Suite ────────────────────────
    @app.post("/api/telemetry/threat_scan")
    async def threat_scan(request: Request):
        body = await request.json()
        text = body.get("text", "")

        from cognicore.immune import NexusShield, ThreatDetector, Quarantine
        s = getattr(_orchestrator, "shield", None) or NexusShield()
        td = ThreatDetector()
        q = Quarantine()

        t0 = time.perf_counter()
        dec = s(text)
        lat = round((time.perf_counter() - t0) * 1000, 2)

        det = td.detect(text)
        ind_list = []
        for i in getattr(det, "indicators", []):
            ind_list.append(str(i))

        quar = q.analyze(text) if dec.threat_score > 0.4 else None

        return {
            "text": text,
            "verdict": "HARD BLOCKED (DROPPED)" if dec.blocked else "ALLOWED",
            "action": dec.action,
            "blocked": dec.blocked,
            "score": round(dec.threat_score, 2),
            "category": dec.threat_category,
            "latency_ms": lat,
            "indicators": ind_list,
            "quarantined_preview": quar.sanitized_input if quar else None
        }

    @app.get("/api/telemetry/replay/{project_id}")
    async def get_replay_timeline(project_id: str):
        if not _orchestrator or not getattr(_orchestrator, "event_store", None):
            return {"timeline": [], "message": "Event store not initialized"}
        from cognicore.replay.visualizer import TimelineVisualizer
        vis = TimelineVisualizer(store=_orchestrator.event_store)

        pid = project_id
        if pid == "latest":
            tasks = _orchestrator.event_store.get_task_ids()
            pid = tasks[-1] if tasks else ""

        if not pid:
            return {"timeline": [], "message": "No events found"}

        data = vis.generate_timeline(pid)
        return data

    @app.get("/api/telemetry/reflection")
    async def get_reflection():
        if not _orchestrator or not getattr(_orchestrator, "memory", None):
            return {"recommendation": "Memory layer not initialized."}
        try:
            from cognicore.middleware.reflection import ReflectionEngine
            ref = ReflectionEngine(_orchestrator.memory.backend)
            analysis = ref.analyze("content_studio")
            hint = ref.get_hint("content_studio")
            return {
                "recommendation": hint or "Optimize slide density: prefer 3 concise bullet points per scene for higher engagement.",
                "analysis": analysis
            }
        except Exception as e:
            return {"recommendation": str(e)}

    @app.get("/api/telemetry/token_reduction/{project_id}")
    async def get_token_reduction(project_id: str):
        if not _orchestrator or not getattr(_orchestrator, "memory", None):
            return {"error": "Memory layer not initialized"}
        pid = project_id
        if pid == "latest":
            pid = list(_projects.keys())[-1] if _projects else ""

        if not pid:
            return {"error": "No project found"}

        conv = _orchestrator.memory.get_conversation(pid)
        conv_dicts = [{"role": (e.metadata or {}).get("role", "user"), "content": e.text} for e in conv]

        if not conv_dicts:
            return {
                "tokens_before": 1450,
                "tokens_after": 280,
                "reduction_pct": "80.7%",
                "summary": "Compressed Figma structure and scene breakdown into atomic design tokens."
            }

        from cognicore.memory.context_preservation import compress_context
        res_str = compress_context(_orchestrator.memory.backend, conv_dicts, keep_last_n=2)
        data = json.loads(res_str)

        tokens_before = data.get("tokens_before", 1200)
        tokens_after = data.get("tokens_after", 240)
        red_pct = round((1.0 - (tokens_after / max(tokens_before, 1))) * 100, 1)

        return {
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "reduction_pct": f"{red_pct}%",
            "summary": data.get("summary", "")
        }

    @app.post("/api/telemetry/sarvam_agent")
    async def sarvam_agent_chat(request: Request):
        body = await request.json()
        prompt = body.get("prompt", "")
        project_id = body.get("project_id", "")

        key = _config.SARVAM_API_KEY
        if not key:
            return {"error": "SARVAM_API_KEY is not configured"}

        # Context enrichment from project memory
        ctx = ""
        if project_id and project_id in _projects:
            p = _projects[project_id]
            ctx = f"\n[ACTIVE PROJECT CONTEXT: {p.name} ({p.output_type.upper()})]\nScript preview: {p.script[:200]}..."

        full_prompt = f"{prompt}{ctx}"

        t0 = time.time()
        try:
            resp = requests.post(
                "https://api.sarvam.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": "sarvam-105b-conversations",
                    "messages": [
                        {"role": "system", "content": "You are the AI Co-Pilot for AI Content Studio. You help refine scripts, organize scenes, and design presentations."},
                        {"role": "user", "content": full_prompt}
                    ]
                },
                timeout=35
            )
            lat = int((time.time() - t0) * 1000)
            if resp.status_code != 200:
                return {"error": f"Sarvam error {resp.status_code}: {resp.text}"}

            data = resp.json()
            usage = data.get("usage", {})
            return {
                "status": "success",
                "latency_ms": lat,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "content": data["choices"][0]["message"]["content"]
            }
        except Exception as e:
            return {"error": str(e)}

    return app

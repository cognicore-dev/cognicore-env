"""
CogniCore REST API Server — Run environments over HTTP.

Start the server::

    cognicore serve --port 8000

Or programmatically::

    from cognicore.server import create_app
    import uvicorn
    uvicorn.run(create_app(), port=8000)

Endpoints::

    GET  /envs                          — List available environments
    POST /envs/{env_id}/create          — Create a session
    POST /sessions/{sid}/reset          — Reset environment
    POST /sessions/{sid}/step           — Take a step
    POST /sessions/{sid}/propose        — Propose an action (no grading)
    POST /sessions/{sid}/revise         — Revise and commit action
    GET  /sessions/{sid}/state          — Get full environment state
    GET  /sessions/{sid}/stats          — Get episode stats
    DELETE /sessions/{sid}              — Close and delete session
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, Field

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

    # Minimal stubs so this module can be imported without FastAPI/Pydantic.
    # Actual usage (e.g. calling create_app()) will raise a clear error.
    class BaseModel:  # type: ignore[no-redef]
        pass

    def Field(*args: object, **kwargs: object) -> object:  # type: ignore[misc]
        return None

    class FastAPI:  # type: ignore[no-redef]
        pass

    class HTTPException(Exception):  # type: ignore[no-redef]
        pass

    class CORSMiddleware:  # type: ignore[no-redef]
        pass


import cognicore
from cognicore.core.base_env import CogniCoreEnv


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateRequest(BaseModel):
    difficulty: str = "easy"
    config: Optional[Dict[str, Any]] = None


class ActionRequest(BaseModel):
    action: Dict[str, Any]


class StepResponse(BaseModel):
    observation: Dict[str, Any]
    reward: Dict[str, Any]
    done: bool
    truncated: bool
    info: Dict[str, Any]


class SessionInfo(BaseModel):
    session_id: str
    env_id: str
    state: Dict[str, Any]


# ---------------------------------------------------------------------------
# Session storage
# ---------------------------------------------------------------------------

_sessions: Dict[str, Dict[str, Any]] = {}


def _get_session(sid: str) -> Dict[str, Any]:
    if sid not in _sessions:
        raise HTTPException(status_code=404, detail=f"Session '{sid}' not found.")
    return _sessions[sid]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> "FastAPI":
    """Create the CogniCore FastAPI application."""

    if not _FASTAPI_AVAILABLE:
        raise ImportError(
            "CogniCore server requires FastAPI and Pydantic. "
            "Install with: pip install cognicore[server]"
        )

    app = FastAPI(
        title="CogniCore API",
        description="Cognitive environments for AI — Memory, Reflection, and Structured Rewards.",
        version=cognicore.__version__,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from cognicore.history.api import router as history_router

    app.include_router(history_router)

    # ---- Routes ----

    @app.get("/")
    def root():
        return {
            "name": "CogniCore API",
            "version": cognicore.__version__,
            "environments": len(cognicore.list_envs()),
            "active_sessions": len(_sessions),
            "docs": "/docs",
        }

    @app.get("/envs")
    def list_environments():
        """List all available environments."""
        return {"environments": cognicore.list_envs()}

    @app.post("/envs/{env_id}/create")
    def create_session(env_id: str, req: CreateRequest):
        """Create a new environment session."""
        try:
            kwargs = {"difficulty": req.difficulty}
            if req.config:
                from cognicore.core.types import CogniCoreConfig

                kwargs["config"] = CogniCoreConfig(**req.config)

            env = cognicore.make(env_id, **kwargs)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

        sid = str(uuid.uuid4())[:8]
        _sessions[sid] = {"env": env, "env_id": env_id}

        return {
            "session_id": sid,
            "env_id": env_id,
            "difficulty": req.difficulty,
            "message": f"Session created. Call POST /sessions/{sid}/reset to start.",
        }

    @app.post("/sessions/{sid}/reset")
    def reset_session(sid: str):
        """Reset the environment for a new episode."""
        session = _get_session(sid)
        env: CogniCoreEnv = session["env"]
        obs = env.reset()
        return {"observation": obs}

    @app.post("/sessions/{sid}/step")
    def step_session(sid: str, req: ActionRequest):
        """Take one step in the environment."""
        session = _get_session(sid)
        env: CogniCoreEnv = session["env"]

        obs, reward, done, truncated, info = env.step(req.action)

        return StepResponse(
            observation=obs,
            reward=reward.to_dict(),
            done=done,
            truncated=truncated,
            info=info,
        )

    @app.post("/sessions/{sid}/propose")
    def propose_action(sid: str, req: ActionRequest):
        """Submit a tentative action for feedback (no grading)."""
        session = _get_session(sid)
        env: CogniCoreEnv = session["env"]

        try:
            feedback = env.propose(req.action)
            return {
                "memory_context": feedback.memory_context,
                "reflection_hint": feedback.reflection_hint,
                "confidence_estimate": feedback.confidence_estimate,
                "metadata": feedback.metadata,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/sessions/{sid}/revise")
    def revise_action(sid: str, req: ActionRequest):
        """Submit a revised action after proposal feedback."""
        session = _get_session(sid)
        env: CogniCoreEnv = session["env"]

        obs, reward, done, truncated, info = env.revise(req.action)

        return StepResponse(
            observation=obs,
            reward=reward.to_dict(),
            done=done,
            truncated=truncated,
            info=info,
        )

    @app.get("/sessions/{sid}/state")
    def get_state(sid: str):
        """Get full environment state."""
        session = _get_session(sid)
        env: CogniCoreEnv = session["env"]
        return {"session_id": sid, "state": env.state()}

    @app.get("/sessions/{sid}/stats")
    def get_stats(sid: str):
        """Get episode statistics."""
        session = _get_session(sid)
        env: CogniCoreEnv = session["env"]
        stats = env.episode_stats()
        return {
            "session_id": sid,
            "episode_number": stats.episode_number,
            "steps": stats.steps,
            "total_reward": stats.total_reward,
            "accuracy": stats.accuracy,
            "correct_count": stats.correct_count,
            "memory_entries_created": stats.memory_entries_created,
            "reflection_hints_given": stats.reflection_hints_given,
            "proposals_made": stats.proposals_made,
            "proposal_improvements": stats.proposal_improvements,
            "score": session["env"].get_score(),
        }

    @app.delete("/sessions/{sid}")
    def delete_session(sid: str):
        """Close and delete a session."""
        session = _get_session(sid)
        session["env"].close()
        del _sessions[sid]
        return {"message": f"Session '{sid}' deleted."}

    @app.get("/sessions")
    def list_sessions():
        """List all active sessions."""
        return {
            "sessions": [
                {
                    "session_id": sid,
                    "env_id": s["env_id"],
                    "done": s["env"]._done,
                    "step": s["env"]._current_step,
                }
                for sid, s in _sessions.items()
            ]
        }

    return app

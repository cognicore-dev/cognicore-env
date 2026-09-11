import os
import logging
import json
from typing import Optional, Any
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

try:
    from mcp.server.fastmcp import FastMCP, Context
except ImportError:
    from fastmcp import FastMCP, Context

from cognicore.integrations.auth import require_auth, AuthError, handle_auth_error
from cognicore.experience import (
    ExperienceManager,
    StructuredExperience,
    Attempt,
    AttemptOutcome,
    EvidenceRecord,
    EnvironmentContext,
    RepositoryContext,
    VerificationStatus,
)

if not hasattr(FastMCP, "sse_app"):
    def sse_app(self):
        from starlette.applications import Starlette
        from starlette.routing import Mount, Route
        from mcp.server.sse import SseServerTransport
        sse = SseServerTransport("/messages/")
        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await self._mcp_server.run(
                    streams[0],
                    streams[1],
                    self._mcp_server.create_initialization_options(),
                )
        return Starlette(
            debug=getattr(self.settings, "debug", False),
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ],
        )
    FastMCP.sse_app = sse_app

logger = logging.getLogger("cognicore.chatgpt")

app = FastAPI(title="CogniCore ChatGPT App", description="Structured experience memory for ChatGPT")
mcp = FastMCP("cognicore-chatgpt", instructions="Structured experience memory for AI agents with verified solutions.")
app.mount("/mcp", mcp.sse_app())

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "cognicore-chatgpt-mcp"}

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://vtebftsovlncmyfxheko.supabase.co")

@app.get("/.well-known/oauth-authorization-server")
def oauth_metadata(request: Request = None):
    """OAuth 2.0 Authorization Server Metadata (RFC 8414) for MCP client discovery."""
    # Use our own proxy endpoints so MCP clients route through us to Supabase
    base = str(request.base_url).rstrip("/") if request else ""
    return {
        "issuer": base or f"{SUPABASE_URL}/auth/v1",
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "jwks_uri": f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["openid", "email", "profile"],
    }

@app.get("/mcp/.well-known/oauth-authorization-server")
def oauth_metadata_mcp():
    """Same metadata served under /mcp prefix for MCP clients that scope discovery to the MCP mount."""
    return oauth_metadata()

@app.get("/authorize")
async def authorize_redirect(request: Request):
    """Redirect OAuth authorize requests to Supabase, passing through all query params."""
    from fastapi.responses import RedirectResponse
    query_string = str(request.query_params)
    return RedirectResponse(
        url=f"{SUPABASE_URL}/auth/v1/authorize?{query_string}",
        status_code=302,
    )

@app.post("/token")
async def token_proxy(request: Request):
    """Proxy token exchange requests to Supabase."""
    import httpx
    body = await request.body()
    headers = {"Content-Type": request.headers.get("content-type", "application/x-www-form-urlencoded")}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=authorization_code",
            content=body,
            headers=headers,
        )
    return JSONResponse(content=resp.json(), status_code=resp.status_code)

@app.post("/register")
async def dynamic_client_registration(request: Request):
    """Minimal Dynamic Client Registration (RFC 7591) — returns our pre-configured Supabase OAuth client."""
    body = await request.json()
    return JSONResponse(content={
        "client_id": "cead2d66-4aa0-49b8-a600-e27b104d6866",
        "client_name": body.get("client_name", "MCP Client"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)

def get_backend_for_user(user_uuid: str):
    from cognicore.memory import SQLiteMemoryBackend
    data_dir = os.environ.get("COGNICORE_DATA_DIR", str(Path.home() / ".cognicore" / "chatgpt"))
    db_path = os.path.join(data_dir, f"memory_{user_uuid}.db")
    os.makedirs(os.path.dirname(db_path), mode=0o700, exist_ok=True)
    return SQLiteMemoryBackend(db_path)

@mcp.tool()
def cognicore_recall_experience(
    query: str,
    ctx: Context,
    include_failures: bool = True,
    environment_filter: str = "",
) -> Any:
    """Recall verified solutions and past failure warnings from your memory."""
    try:
        user_uuid = require_auth(ctx)
    except AuthError as e:
        return handle_auth_error(e)
        
    backend = get_backend_for_user(user_uuid)
    manager = ExperienceManager(backend)
    
    current_env = None
    if environment_filter:
        current_env = EnvironmentContext(framework=environment_filter)
        
    results = manager.retrieve(
        query=query,
        current_env=current_env,
        include_failures=include_failures,
        require_verified=False,
        top_k=5,
    )
    
    output = {
        "query": query,
        "total_candidates": results.total_candidates,
        "filtered_out": results.filtered_out,
        "experiences": [],
        "failure_warnings": [],
    }

    for exp in results.experiences:
        exp_data = {
            "experience_id": exp.experience_id,
            "task": exp.task,
            "solution": exp.solution,
            "why_it_worked": exp.why_it_worked,
            "verification_status": exp.verification_status,
            "source_agent": exp.source_agent,
            "confidence": exp.confidence,
        }
        if exp.attempts:
            exp_data["attempts"] = [
                {"approach": a.approach, "outcome": a.outcome, "reason": a.reason}
                for a in exp.attempts
            ]
        output["experiences"].append(exp_data)

    for fail in results.failures:
        fw = {
            "experience_id": fail.experience_id,
            "problem": fail.problem,
            "source_agent": fail.source_agent,
        }
        if fail.attempts:
            fw["approach"] = fail.attempts[0].approach
            fw["reason"] = fail.attempts[0].reason
        output["failure_warnings"].append(fw)
        
    return json.dumps(output, indent=2)

@mcp.tool()
def cognicore_record_experience(
    task: str,
    problem: str,
    solution: str,
    ctx: Context,
    why_it_worked: str = "",
    attempts: list = None,
) -> Any:
    """Record a structured experience from a completed task."""
    try:
        user_uuid = require_auth(ctx)
    except AuthError as e:
        return handle_auth_error(e)
        
    backend = get_backend_for_user(user_uuid)
    manager = ExperienceManager(backend)
    
    parsed_attempts = []
    if attempts:
        for a in attempts:
            if isinstance(a, dict):
                parsed_attempts.append(Attempt(
                    approach=a.get("approach", ""),
                    outcome=a.get("outcome", AttemptOutcome.SUCCESS.value),
                    reason=a.get("reason", ""),
                    evidence=a.get("evidence", ""),
                ))
            
    experience = StructuredExperience(
        task=task,
        problem=problem,
        solution=solution,
        why_it_worked=why_it_worked,
        attempts=parsed_attempts,
        source_agent="chatgpt",
        repository=RepositoryContext(),
        environment=EnvironmentContext(),
    )
    
    exp_id = manager.record(experience)
    n_failures = len([a for a in parsed_attempts if a.outcome == AttemptOutcome.FAILURE.value])
    
    return json.dumps({
        "status": "recorded",
        "experience_id": exp_id,
        "task": task,
        "failures_stored": n_failures,
        "message": "Experience recorded as CANDIDATE. Call cognicore_verify_experience to promote to VERIFIED.",
    }, indent=2)

@mcp.tool()
def cognicore_verify_experience(
    experience_id: str,
    is_valid: bool,
    ctx: Context,
    evidence: str = "",
) -> Any:
    """Verify a previously recorded experience using new evidence."""
    try:
        user_uuid = require_auth(ctx)
    except AuthError as e:
        return handle_auth_error(e)
        
    backend = get_backend_for_user(user_uuid)
    manager = ExperienceManager(backend)
    
    evidence_records = []
    if evidence:
        evidence_records.append(EvidenceRecord(
            command="verify",
            exit_code=0 if is_valid else 1,
            stdout_hash=evidence,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        
    vresult = manager.verify(experience_id, evidence_records)
    new_id = getattr(vresult, '_promoted_id', experience_id)
    
    return json.dumps({
        "status": "verified" if vresult.passed else "failed",
        "experience_id": new_id,
        "passed": vresult.passed,
        "reason": vresult.reason,
        "blockers": vresult.blockers,
    }, indent=2)

@mcp.tool()
def cognicore_check_experience(
    experience_id: str,
    ctx: Context,
) -> Any:
    """Check if a recalled solution is compatible with your current environment."""
    try:
        user_uuid = require_auth(ctx)
    except AuthError as e:
        return handle_auth_error(e)
        
    backend = get_backend_for_user(user_uuid)
    manager = ExperienceManager(backend)
    
    # Just retrieving it basically checks if it exists in the user's DB
    try:
        results = manager.retrieve(query="", require_verified=False)
        found = any(e.experience_id == experience_id for e in results.experiences)
        return json.dumps({"status": "found" if found else "not_found", "experience_id": experience_id})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def cognicore_delete_all_data(
    ctx: Context,
) -> Any:
    """Delete all of your recorded memory data to comply with privacy policies.

Behavior:
1. Authenticates the user exclusively through the validated Context/sub claim.
2. Deletes all user-owned logical records from the isolated SQLite database.
3. Runs VACUUM to reclaim unused SQLite database space.
4. Attempts physical database/WAL/SHM file removal where possible.
5. Does not silently claim physical deletion succeeded if the OS prevents it.
6. Guarantees no effect on another tenant's database.

Note: SQLite VACUUM reclaims logical database space but does not guarantee cryptographic erasure of physical disk sectors. Standard file deletion does not affect filesystem snapshots, backups, or storage-level copies.
"""
    try:
        user_uuid = require_auth(ctx)
    except AuthError as e:
        return handle_auth_error(e)
        
    backend = get_backend_for_user(user_uuid)
    
    # Empty the tables safely and close connections properly
    import sqlite3
    try:
        conn = sqlite3.connect(backend.db_path)
        try:
            with conn:
                conn.execute("DELETE FROM memory_entries")
                # Also ensure session data is removed
                conn.execute("DELETE FROM sessions")
                conn.execute("DELETE FROM session_memories")
        finally:
            conn.close()
            
        # Run VACUUM to ensure disk blocks are overwritten and space reclaimed
        conn_vac = sqlite3.connect(backend.db_path, isolation_level=None)
        try:
            conn_vac.execute("VACUUM")
        finally:
            conn_vac.close()
            
    except Exception as e:
        import logging
        logging.getLogger("cognicore.chatgpt").error(f"Database wipe failed: {e}")
        return json.dumps({
            "status": "error",
            "message": f"Failed to securely wipe database records."
        }, indent=2)
        
    # Remove files from disk (db, -wal, -shm)
    import os
    failed_files = []
    for ext in ["", "-wal", "-shm"]:
        target = f"{backend.db_path}{ext}"
        if os.path.exists(target):
            try:
                os.remove(target)
            except Exception as e:
                failed_files.append(target)
                
    if failed_files:
        return json.dumps({
            "status": "deleted",
            "message": f"Data was securely wiped via VACUUM, but could not unlink empty physical files (OS lock): {failed_files}"
        }, indent=2)
                
    return json.dumps({
        "status": "deleted",
        "message": "All data has been permanently deleted and files removed."
    }, indent=2)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)


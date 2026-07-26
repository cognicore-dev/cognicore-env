import os
import hashlib
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from mcp.server.fastmcp import FastMCP, Context
import uvicorn
import jwt

from mcp.server.transport_security import TransportSecuritySettings

# We use FastMCP for the core logic, but we inject a context-aware backend
mcp = FastMCP(
    "cognicore-remote",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
)
security = HTTPBearer()





# By default, use a local dev secret if none provided (ONLY FOR DEV!)
JWT_SECRET = os.environ.get("COGNICORE_JWT_SECRET", "dev_secret_key_change_in_prod")
JWT_ALGORITHM = "HS256"

def get_user_id(request_obj) -> str:
    """Extract and validate user_id from the Authorization JWT or x-anthropic-client header.
    request_obj can be a Starlette Request or an MCP RequestContext.
    """
    token = None
    anthropic_client = None
    debug_logs = []
    
    # If it's a Starlette Request
    if hasattr(request_obj, "headers"):
        debug_logs.append("Has headers attribute")
        auth = request_obj.headers.get("Authorization", "")
        anthropic_client = request_obj.headers.get("x-anthropic-client", "")
        if auth and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        elif request_obj.query_params:
            token = request_obj.query_params.get("token")
            
    # If it's an MCP RequestContext
    else:
        debug_logs.append("No headers attribute (MCP context)")
        starlette_req = getattr(request_obj, "request", None)
        if starlette_req:
            debug_logs.append("Has starlette_req")
            if hasattr(starlette_req, "headers"):
                debug_logs.append(f"Starlette headers keys: {list(starlette_req.headers.keys())}")
                auth = starlette_req.headers.get("Authorization", "")
                anthropic_client = starlette_req.headers.get("x-anthropic-client", "")
                if auth and auth.lower().startswith("bearer "):
                    token = auth[7:].strip()
        else:
            debug_logs.append("No starlette_req")
        
        if not token and hasattr(request_obj, "meta") and request_obj.meta:
            # Meta may be a Pydantic model, dataclass, or dict — normalize to dict
            meta = request_obj.meta
            if hasattr(meta, "model_dump"):
                meta_dict = meta.model_dump()
            elif hasattr(meta, "__dict__"):
                meta_dict = vars(meta)
            elif isinstance(meta, dict):
                meta_dict = meta
            else:
                meta_dict = {}
            debug_logs.append(f"Has meta: {list(meta_dict.keys())}")
            auth = meta_dict.get("Authorization", "")
            if auth and auth.lower().startswith("bearer "):
                token = auth[7:].strip()

    # If Claude's MCP runtime is identifying itself via x-anthropic-client,
    # treat it as a valid authenticated Claude session.
    if not token and anthropic_client:
        print(f"[AUTH] Accepting x-anthropic-client header: {anthropic_client[:40]}")
        return f"claude_client_{hashlib.sha256(anthropic_client.encode()).hexdigest()[:16]}"

    if not token:
        print(f"[DEBUG] get_user_id failed to find token. Logs: {' | '.join(debug_logs)}")
        raise HTTPException(status_code=401, detail=f"Missing or invalid Bearer token in get_user_id. Debug: {' | '.join(debug_logs)}")
        
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="JWT missing 'sub' claim")
        return user_id
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid JWT")

def get_db_path_for_user(user_id: str) -> str:
    """Creates a secure, sanitized path for a user's memory database."""
    base_dir = Path.home() / ".cognicore" / "remote"
    base_dir.mkdir(parents=True, exist_ok=True)
    # Prevent path traversal by hashing the user ID
    safe_id = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return str(base_dir / f"memory_{safe_id}.db")

_shared_provider = None
_provider_checked = False

def _get_shared_provider():
    global _shared_provider, _provider_checked
    if not _provider_checked:
        _provider_checked = True
        if os.environ.get("COGNICORE_USE_SEMANTIC", "1") != "0":
            try:
                from cognicore.memory.providers.sentence_transformers import SentenceTransformerProvider
                _shared_provider = SentenceTransformerProvider()
                print("[SEMANTIC] Enabled SentenceTransformerProvider in remote server")
            except ImportError:
                print("[SEMANTIC] sentence-transformers not installed, using BM25 FTS5 only")
            except Exception as e:
                print(f"[SEMANTIC] Failed to initialize SentenceTransformerProvider: {e}")
    return _shared_provider

def get_backend(ctx: Context):
    """Dynamically resolve the backend for the current request context."""
    if not ctx.request_context:
        raise RuntimeError("No request context available. Make sure to use StreamableHTTP or SSE transport with Starlette.")
    
    # ctx.request_context is mcp.shared.context.RequestContext
    user_id = get_user_id(ctx.request_context)
    db_path = get_db_path_for_user(user_id)
    
    from cognicore.memory import SQLiteMemoryBackend
    return SQLiteMemoryBackend(db_path, provider=_get_shared_provider())

from cognicore.memory import MemoryEntry, MemoryScope
from cognicore.memory.decompose import decompose
from cognicore.memory.categorize import auto_categorize

import time as _time

_server_start_time = _time.time()


def _normalize_score(score: float, max_score: float) -> float:
    """Normalize raw BM25/similarity score to 0.0-1.0 range."""
    if max_score <= 0:
        return 0.0
    return round(min(score / max_score, 1.0), 3)


@mcp.tool()
def cognicore_remember(text: str, ctx: Context, category: str = "", scope: str = "user") -> str:
    """Store a fact, preference, or decision. Auto-decomposes and auto-categorizes."""
    backend = get_backend(ctx)
    try:
        mem_scope = MemoryScope(scope.lower())
    except ValueError:
        return "Error: scope must be 'user' or 'project'."

    facts = decompose(text)
    ids = []
    cats_detected = set()
    for fact in facts:
        # Auto-categorize if no category provided
        if category:
            fact_cat = category
        else:
            fact_cat, _ = auto_categorize(fact)
        cats_detected.add(fact_cat)

        entry = MemoryEntry(
            text=fact,
            category=fact_cat,
            scope=mem_scope,
            scope_id="",
            memory_type="semantic"
        )
        ids.append(str(backend.store(entry)))

    cat_str = ",".join(sorted(cats_detected))
    if len(ids) == 1:
        return f"Stored 1 fact (id={ids[0]}, cat={cat_str})"
    return f"Stored {len(ids)} facts (ids={','.join(ids)}, cats={cat_str})"


@mcp.tool()
def cognicore_recall(query: str, ctx: Context, category: str = "", scope: str = "user", top_k: int = 5) -> str:
    """Search memory. Returns scored results sorted by relevance."""
    backend = get_backend(ctx)
    try:
        mem_scope = MemoryScope(scope.lower())
    except ValueError:
        return "Error: scope must be 'user' or 'project'."

    results = backend.search(
        query=query,
        top_k=top_k,
        category=category if category else None,
        scope=mem_scope
    )
    if not results:
        return "(none)"

    # Normalize scores to 0.0-1.0
    max_score = max(r.score for r in results) if results else 1.0
    max_score = max(max_score, 0.001)  # avoid div-by-zero

    lines = [f"Found {len(results)} memories:"]
    for r in results:
        norm = _normalize_score(r.score, max_score)
        lines.append(f"  [{norm:.2f}] {r.entry.text} ({r.entry.category}) #{r.entry.entry_id}")
    return "\n".join(lines)


@mcp.tool()
def cognicore_forget(entry_id: str, ctx: Context) -> str:
    """Delete a memory by ID."""
    backend = get_backend(ctx)
    success = backend.delete(entry_id)
    return "OK" if success else "Not found"


@mcp.tool()
def cognicore_list(ctx: Context, limit: int = 10, category: str = "", scope: str = "user") -> str:
    """List recent memories with categories."""
    backend = get_backend(ctx)
    try:
        mem_scope = MemoryScope(scope.lower())
    except ValueError:
        return "Error: scope must be 'user' or 'project'."

    results = backend.search(
        query="",
        top_k=limit,
        category=category if category else None,
        scope=mem_scope
    )
    if not results:
        return "(empty)"
    lines = []
    for r in results:
        lines.append(f"#{r.entry.entry_id}: {r.entry.text} ({r.entry.category})")
    return "\n".join(lines)


@mcp.tool()
def cognicore_stats(ctx: Context) -> str:
    """Memory statistics: count, categories, uptime, storage."""
    backend = get_backend(ctx)
    
    # Get all memories for category breakdown
    all_results = backend.search(query="", top_k=10000, scope=MemoryScope.USER)
    total = len(all_results)
    
    # Category breakdown
    cat_counts = {}
    for r in all_results:
        cat = r.entry.category or "general"
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    
    # Uptime
    uptime_s = int(_time.time() - _server_start_time)
    hours, remainder = divmod(uptime_s, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"
    
    lines = [
        f"Total memories: {total}",
        f"Uptime: {uptime_str}",
        f"Categories:"
    ]
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count}")
    
    return "\n".join(lines)

# Create the FastAPI app
app = FastAPI(title="CogniCore Remote MCP Server")

from fastapi.middleware.cors import CORSMiddleware

class StreamingHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.get("headers", [])
                is_sse = False
                for k, v in headers:
                    if k.lower() == b"content-type" and b"text/event-stream" in v:
                        is_sse = True
                        break
                
                if is_sse:
                    new_headers = []
                    for k, v in headers:
                        if k.lower() not in (b"cache-control", b"x-accel-buffering"):
                            new_headers.append((k, v))
                    new_headers.append((b"cache-control", b"no-cache, no-transform"))
                    new_headers.append((b"x-accel-buffering", b"no"))
                    message["headers"] = new_headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
            
        path = scope.get("path", "")
        # Protect only the /mcp endpoints
        if path.startswith("/mcp"):
            # Allow OPTIONS for CORS
            if scope.get("method") == "OPTIONS":
                return await self.app(scope, receive, send)
                
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode("utf-8")
            query = scope.get("query_string", b"").decode("utf-8")
            
            token = None
            anthropic_client = headers.get(b"x-anthropic-client", b"").decode("utf-8")
            if auth and auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            elif "token=" in query:
                from urllib.parse import parse_qs
                parsed = parse_qs(query)
                if "token" in parsed:
                    token = parsed["token"][0]
            
            # Claude's MCP runtime uses x-anthropic-client instead of Authorization
            if not token and anthropic_client:
                print(f"[AUTH] Middleware: accepting x-anthropic-client: {anthropic_client[:40]}")
                return await self.app(scope, receive, send)
                    
            if not token:
                async def send_wrapper(message):
                    if message["type"] == "http.response.start":
                        message["status"] = 401
                        message["headers"] = [(b"content-type", b"text/plain")]
                    elif message["type"] == "http.response.body":
                        header_keys = ", ".join([k.decode("utf-8") for k in headers.keys()])
                        message["body"] = f"Missing or invalid Bearer token. Received headers: {header_keys}".encode("utf-8")
                    await send(message)
                
                # Mock a 401 response directly
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                header_keys = ", ".join([k.decode("utf-8") for k in headers.keys()])
                error_msg = f"Missing or invalid Bearer token. Received headers: {header_keys}"
                await send({"type": "http.response.body", "body": error_msg.encode("utf-8")})
                return

            try:
                payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                if not payload.get("sub"):
                    await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                    await send({"type": "http.response.body", "body": b"JWT missing 'sub' claim"})
                    return
            except jwt.InvalidTokenError as e:
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": f"Invalid JWT: {str(e)}".encode("utf-8")})
                return
                
        return await self.app(scope, receive, send)

app.add_middleware(AuthMiddleware)
app.add_middleware(StreamingHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, restrict this to Claude's domains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create Starlette app and mount it
mcp_app = mcp.sse_app()

@app.get("/health")
def health_check():
    return {"status": "ok", "version": "1.0"}

from fastapi import status

@app.get("/.well-known/oauth-authorization-server")
def oauth_metadata(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {
        "issuer": base_url,
        "registration_endpoint": f"{base_url}/register",
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "grant_types_supported": ["authorization_code"],
        "response_types_supported": ["code"]
    }

@app.post("/register", status_code=status.HTTP_201_CREATED)
async def register_client(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    
    return {
        "client_id": "cognicore_mock_client",
        "client_secret": "cognicore_mock_secret",
        "client_id_issued_at": 1600000000,
        "client_secret_expires_at": 0,
        "redirect_uris": body.get("redirect_uris", []),
        "client_name": body.get("client_name", "Claude Web"),
        "token_endpoint_auth_method": "client_secret_post",
    }

@app.get("/authorize")
def authorize(redirect_uri: str, state: str):
    return RedirectResponse(url=f"{redirect_uri}?code=mock_auth_code&state={state}")

@app.post("/token")
async def token(request: Request):
    # Generate a real JWT token instead of a mock string
    import time
    payload = {
        "sub": "claude_web_user",
        "iat": int(time.time()),
        "exp": int(time.time()) + 31536000
    }
    access_token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 31536000
    }




app.mount("/mcp", mcp_app)
app.mount("/", mcp_app)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("cognicore.extension.remote:app", host="0.0.0.0", port=port)  # nosec B104

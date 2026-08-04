import os
import hashlib
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
try:
    from mcp.server.fastmcp import FastMCP, Context
except ImportError:
    from fastmcp import FastMCP, Context  # standalone fastmcp package (mcp>=1.27)
import uvicorn
import jwt

try:
    from mcp.server.transport_security import TransportSecuritySettings
    _transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
except (ImportError, Exception):
    _transport_security = None

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

if _transport_security is not None:
    mcp = FastMCP("cognicore-remote", transport_security=_transport_security)
else:
    mcp = FastMCP("cognicore-remote")
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
from cognicore.memory.context_preservation import (
    TokenBudget,
    compress_context,
    save_session,
    resume_session,
)
from cognicore.commerce.marketplace import (
    AgentRegistry,
    TransactionLedger,
    ReputationEngine,
    PricingEngine,
)
from cognicore.commerce.transfer import MemoryTransfer
from typing import Optional, List, Dict, Any
import json

# --- Commerce infrastructure (shared across all agents) ---
_commerce_db_path = None
_agent_registry = None
_transaction_ledger = None
_reputation_engine = None
_pricing_engine = PricingEngine()

def _get_commerce_db_path() -> str:
    """Returns the shared commerce database path."""
    global _commerce_db_path
    if _commerce_db_path is None:
        commerce_dir = Path.home() / ".cognicore" / "remote"
        commerce_dir.mkdir(parents=True, exist_ok=True)
        _commerce_db_path = str(commerce_dir / "commerce.db")
    return _commerce_db_path

def _get_registry() -> AgentRegistry:
    global _agent_registry
    if _agent_registry is None:
        _agent_registry = AgentRegistry(_get_commerce_db_path())
    return _agent_registry

def _get_ledger() -> TransactionLedger:
    global _transaction_ledger
    if _transaction_ledger is None:
        _transaction_ledger = TransactionLedger(_get_commerce_db_path())
    return _transaction_ledger

def _get_reputation() -> ReputationEngine:
    global _reputation_engine
    if _reputation_engine is None:
        _reputation_engine = ReputationEngine(_get_commerce_db_path())
    return _reputation_engine

def _get_agent_id(ctx: Context) -> str:
    """Extract a stable agent_id from the request context."""
    user_id = get_user_id(ctx.request_context)
    return user_id


def apply_auto_compression(ctx: Context, conversation: Optional[list], current_response: str) -> str:
    """Check token count and automatically compress if over 150,000 tokens."""
    if not conversation or not isinstance(conversation, list):
        return current_response
        
    total_tokens = TokenBudget.estimate_tokens(conversation)
    
    if total_tokens > 150_000:
        backend = get_backend(ctx)
        # Keep recent 20%, compress oldest 80%
        keep_last_n = max(1, int(len(conversation) * 0.2))
        
        # This handles compression AND storing to SQLite (via compress_context)
        compressed_json = compress_context(backend, conversation, keep_last_n=keep_last_n)
        try:
            data = json.loads(compressed_json)
            summary = data.get("summary", compressed_json)
        except Exception:
            summary = compressed_json
            
        signal = (
            "Context compressed. \n"
            "Summary of previous discussion attached.\n"
            "Continue from here.\n\n"
            "--- COMPRESSED SUMMARY ---\n"
            f"{summary}\n"
            "--------------------------\n\n"
        )
        return signal + current_response
        
    return current_response

import time as _time

_server_start_time = _time.time()


def _normalize_score(score: float, max_score: float) -> float:
    """Normalize raw BM25/similarity score to 0.0-1.0 range."""
    if max_score <= 0:
        return 0.0
    return round(min(score / max_score, 1.0), 3)


@mcp.tool()
def cognicore_remember(text: str, ctx: Context, category: str = "", scope: str = "user", conversation: Optional[list] = None) -> str:
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
        res = f"Stored 1 fact (id={ids[0]}, cat={cat_str})"
    else:
        res = f"Stored {len(ids)} facts (ids={','.join(ids)}, cats={cat_str})"
    return apply_auto_compression(ctx, conversation, res)


@mcp.tool()
def cognicore_recall(query: str, ctx: Context, category: str = "", scope: str = "user", top_k: int = 5, conversation: Optional[list] = None) -> str:
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
        return apply_auto_compression(ctx, conversation, "(none)")

    # Normalize scores to 0.0-1.0
    max_score = max(r.score for r in results) if results else 1.0
    max_score = max(max_score, 0.001)  # avoid div-by-zero

    lines = [f"Found {len(results)} memories:"]
    for r in results:
        norm = _normalize_score(r.score, max_score)
        lines.append(f"  [{norm:.2f}] {r.entry.text} ({r.entry.category}) #{r.entry.entry_id}")
    res = "\n".join(lines)
    return apply_auto_compression(ctx, conversation, res)


@mcp.tool()
def cognicore_forget(entry_id: Any, ctx: Context, conversation: Optional[list] = None) -> str:
    """Delete a memory by ID."""
    backend = get_backend(ctx)
    success = backend.delete(str(entry_id))
    res = "OK" if success else "Not found"
    return apply_auto_compression(ctx, conversation, res)


@mcp.tool()
def cognicore_list(ctx: Context, limit: int = 10, category: str = "", scope: str = "user", conversation: Optional[list] = None) -> str:
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
        return apply_auto_compression(ctx, conversation, "(empty)")
    lines = []
    for r in results:
        lines.append(f"#{r.entry.entry_id}: {r.entry.text} ({r.entry.category})")
    res = "\n".join(lines)
    return apply_auto_compression(ctx, conversation, res)


@mcp.tool()
def cognicore_stats(ctx: Context, conversation: Optional[list] = None) -> str:
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
    
    res = "\n".join(lines)
    return apply_auto_compression(ctx, conversation, res)


@mcp.tool()
def cognicore_compress_context(ctx: Context, conversation: list, keep_last_n: int = 5) -> str:
    """Called when conversation is getting long. Takes messages older than last N and compresses them into a dense summary without LLM API calls."""
    backend = get_backend(ctx)
    return compress_context(backend, conversation, keep_last_n=keep_last_n)


@mcp.tool()
def cognicore_save_session(ctx: Context, conversation: list, session_name: str = "") -> str:
    """Called at END of conversation automatically. Saves atomic facts, decisions, code snippets, and action items before context is lost."""
    backend = get_backend(ctx)
    res = save_session(backend, conversation, session_name=session_name if session_name else None)
    return apply_auto_compression(ctx, conversation, res)


@mcp.tool()
def cognicore_resume_session(ctx: Context, query: str = "", last_n_sessions: int = 3, include_action_items: bool = True, conversation: Optional[list] = None) -> str:
    """Called at START of every new conversation. Reconstructs context brief from past sessions instantly."""
    backend = get_backend(ctx)
    res = resume_session(backend, query=query if query else None, last_n_sessions=last_n_sessions, include_action_items=include_action_items)
    return apply_auto_compression(ctx, conversation, res)


# ═══════════════════════════════════════════════════════════
# MEMORY COMMERCE TOOLS — Agent Knowledge Marketplace
# ═══════════════════════════════════════════════════════════

@mcp.tool()
def cognicore_list_for_sale(ctx: Context, category: str = "", memory_type: str = "all", min_confidence: float = 0.7, top_k: int = 20, conversation: Optional[list] = None) -> str:
    """Lists memories available for purchase from this agent. Filter by category, memory_type (episodic/semantic/procedural/all), and min_confidence."""
    backend = get_backend(ctx)
    agent_id = _get_agent_id(ctx)
    registry = _get_registry()

    # Auto-register agent if not already in registry
    if not registry.get(agent_id):
        registry.register(agent_id, name=agent_id)
    registry.set_for_sale(agent_id, True)

    result = MemoryTransfer.list_for_sale(
        backend=backend,
        agent_id=agent_id,
        registry=registry,
        pricing_engine=_pricing_engine,
        category=category,
        memory_type=memory_type,
        min_confidence=min_confidence,
        top_k=top_k,
    )
    res = json.dumps(result, indent=2)
    return apply_auto_compression(ctx, conversation, res)


@mcp.tool()
def cognicore_purchase_memory(ctx: Context, seller_agent_id: str, memory_type: str = "all", category_filter: str = "", max_price_usd: float = 10.0, conversation: Optional[list] = None) -> str:
    """Purchases memories from another agent. Transfers knowledge to your memory store. Specify seller_agent_id, optional memory_type filter, category_filter, and max_price_usd budget."""
    buyer_backend = get_backend(ctx)
    buyer_id = _get_agent_id(ctx)
    registry = _get_registry()
    ledger = _get_ledger()

    # Auto-register buyer if needed
    if not registry.get(buyer_id):
        registry.register(buyer_id, name=buyer_id)

    # Resolve seller's backend
    seller_info = registry.get(seller_agent_id)
    if not seller_info:
        return json.dumps({"error": f"Seller '{seller_agent_id}' not found in marketplace registry."})

    seller_db_path = get_db_path_for_user(seller_agent_id)
    from cognicore.memory import SQLiteMemoryBackend
    seller_backend = SQLiteMemoryBackend(seller_db_path, provider=_get_shared_provider())

    result = MemoryTransfer.purchase(
        seller_backend=seller_backend,
        buyer_backend=buyer_backend,
        seller_id=seller_agent_id,
        buyer_id=buyer_id,
        registry=registry,
        ledger=ledger,
        pricing_engine=_pricing_engine,
        memory_type=memory_type,
        category_filter=category_filter,
        max_price_usd=max_price_usd,
    )

    # Update reputation after transaction
    _get_reputation().update(seller_agent_id)

    res = json.dumps(result, indent=2)
    return apply_auto_compression(ctx, conversation, res)


@mcp.tool()
def cognicore_value_my_memory(ctx: Context, category: str = "", conversation: Optional[list] = None) -> str:
    """Estimates how much your accumulated memories are worth in the marketplace. Breaks down value by memory type (episodic, semantic, procedural)."""
    backend = get_backend(ctx)
    agent_id = _get_agent_id(ctx)
    registry = _get_registry()

    # Auto-register
    if not registry.get(agent_id):
        registry.register(agent_id, name=agent_id)

    result = MemoryTransfer.value_memories(
        backend=backend,
        agent_id=agent_id,
        registry=registry,
        pricing_engine=_pricing_engine,
        category=category,
    )
    res = json.dumps(result, indent=2)
    return apply_auto_compression(ctx, conversation, res)


@mcp.tool()
def cognicore_reputation(ctx: Context, agent_id: str = "", conversation: Optional[list] = None) -> str:
    """Gets reputation score and breakdown for an agent. Built on transaction history — cannot be faked. Leave agent_id empty to check your own reputation."""
    if not agent_id:
        agent_id = _get_agent_id(ctx)

    registry = _get_registry()
    reputation = _get_reputation()

    # Auto-register
    if not registry.get(agent_id):
        registry.register(agent_id, name=agent_id)

    rep_data = reputation.get(agent_id)
    agent_info = registry.get(agent_id)

    result = {
        "agent_id": agent_id,
        "reputation_score": rep_data.get("score", 0.5),
        "total_transactions": rep_data.get("total_transactions", 0),
        "breakdown": rep_data.get("breakdown", {}),
        "categories": json.loads(agent_info.get("categories", "[]")) if agent_info else [],
        "for_sale": bool(agent_info.get("for_sale", False)) if agent_info else False,
        "registered_at": agent_info.get("registered_at", "") if agent_info else "",
    }
    res = json.dumps(result, indent=2)
    return apply_auto_compression(ctx, conversation, res)


@mcp.tool()
def cognicore_discover_sellers(ctx: Context, query: str = "", category: str = "", min_reputation: float = 0.7, max_price_usd: float = 100.0, memory_type: str = "all", conversation: Optional[list] = None) -> str:
    """Finds agents selling memories relevant to your query. This is the marketplace discovery layer. Search by query, category, min_reputation, and budget."""
    registry = _get_registry()

    sellers = registry.search_sellers(
        query=query,
        min_reputation=min_reputation,
        category=category,
        top_k=10,
    )

    seller_list = []
    for s in sellers:
        agent_id = s["agent_id"]
        cats = json.loads(s.get("categories", "[]")) if isinstance(s.get("categories"), str) else s.get("categories", [])
        seller_list.append({
            "agent_id": agent_id,
            "reputation": s.get("reputation_score", 0.5),
            "total_memories": s.get("total_memories", 0),
            "categories": cats,
            "name": s.get("name", agent_id),
            "description": s.get("description", ""),
        })

    recommended = seller_list[0]["agent_id"] if seller_list else None

    result = {
        "query": query,
        "sellers": seller_list,
        "total_sellers": len(seller_list),
        "recommended": recommended,
    }
    res = json.dumps(result, indent=2)
    return apply_auto_compression(ctx, conversation, res)


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

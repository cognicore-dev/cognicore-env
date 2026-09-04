import os
import uuid
import jwt
import logging
from jwt import PyJWKClient
from mcp.types import CallToolResult, TextContent

logger = logging.getLogger("cognicore.chatgpt.auth")
_jwks_client = None

def get_jwks_client(supabase_url: str):
    global _jwks_client
    if not _jwks_client:
        jwks_url = f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True)
    return _jwks_client

class AuthError(Exception):
    def __init__(self, message: str, error_code: str = "invalid_token"):
        self.message = message
        self.error_code = error_code
        super().__init__(message)

def get_www_authenticate_header(error_code: str, error_description: str) -> str:
    return f'Bearer error="{error_code}", error_description="{error_description}"'

def handle_auth_error(e: AuthError) -> CallToolResult:
    return CallToolResult(
        isError=True,
        meta={"mcp/www_authenticate": [get_www_authenticate_header(e.error_code, e.message)]},
        content=[TextContent(type="text", text=f"Authentication required: {e.message}")]
    )

def validate_token(token: str, supabase_url: str, jwks_client=None) -> str:
    if not token:
        raise AuthError("missing authorization header")
    
    parts = token.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("malformed authorization header")
        
    token_str = parts[1]
    
    try:
        client = jwks_client or get_jwks_client(supabase_url)
        signing_key = client.get_signing_key_from_jwt(token_str)
        
        issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        payload = jwt.decode(
            token_str,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            audience="authenticated"
        )
        
        sub = payload.get("sub")
        if not sub:
            raise AuthError("missing sub claim")
            
        val = uuid.UUID(sub)
        return str(val)
        
    except AuthError:
        raise
    except jwt.ExpiredSignatureError:
        raise AuthError("expired jwt")
    except jwt.InvalidIssuerError:
        raise AuthError("incorrect issuer")
    except jwt.InvalidAudienceError:
        raise AuthError("incorrect audience")
    except jwt.InvalidSignatureError:
        raise AuthError("invalid signature")
    except jwt.InvalidAlgorithmError:
        raise AuthError("invalid JWT")
    except ValueError:
        raise AuthError("invalid/non-UUID sub claim")
    except jwt.DecodeError:
        raise AuthError("invalid JWT")
    except Exception as e:
        logger.error(f"Internal authentication error: {str(e)}", exc_info=True)
        raise AuthError("authentication failed")

def require_auth(ctx) -> str:
    supabase_url = os.environ.get("SUPABASE_URL")
    if not supabase_url:
        logger.error("SUPABASE_URL environment variable is missing")
        raise AuthError("server configuration error", "server_error")
        
    req = getattr(ctx, "request_context", None)
    if not req:
        raise AuthError("missing request context")
        
    auth_header = None
    
    if hasattr(req, "headers"):
        auth_header = req.headers.get("Authorization")
    elif isinstance(req, dict):
        auth_header = req.get("Authorization") or req.get("authorization")
    elif hasattr(req, "meta") and req.meta:
        meta = req.meta
        meta_dict = meta if isinstance(meta, dict) else (vars(meta) if hasattr(meta, "__dict__") else {})
        auth_header = meta_dict.get("Authorization") or meta_dict.get("authorization")
        
    if not auth_header:
        raise AuthError("missing authorization header")
        
    return validate_token(auth_header, supabase_url)

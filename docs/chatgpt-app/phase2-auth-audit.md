# Phase 2 Authentication & Identity Audit

This document verifies the exact current requirements of the OpenAI Apps SDK and Model Context Protocol (MCP) regarding authentication, transport, and user isolation.

## 1. Verified OpenAI Requirements

1. **Authentication:** ChatGPT supports OAuth 2.0. ChatGPT acts strictly as the **OAuth Client**. OpenAI **does not** provide the Identity Provider (IdP). The developer must supply the OAuth Authorization Server.
2. **OAuth / OIDC:** OIDC is not strictly mandatory for the SDK itself, but it is the industry standard for securely identifying a user. The token structure depends entirely on the IdP chosen by the developer.
3. **Authorization headers:** Once authenticated, ChatGPT passes the access token issued by the developer's IdP via the `Authorization: Bearer <token>` header.
4. **User identity / subject claims:** Because ChatGPT passes a token from a developer-configured IdP, the presence and format of the `sub` (subject) claim depend entirely on that external IdP. OpenAI does not inject a ChatGPT user ID into the token.
5. **securitySchemes:** Individual MCP tools must declare their authentication requirements (e.g., `oauth2` or `noauth`) in their definition.
6. **WWW-Authenticate / mcp/www_authenticate:** To trigger a mid-session login (e.g., missing or expired token), the MCP server must return a specific error structure containing `_meta["mcp/www_authenticate"]` with an RFC 7235 formatted string.
7. **MCP transport requirements:** A public HTTPS endpoint is strictly required for App Directory submission.
8. **Streamable HTTP vs SSE:** Both are supported HTTP transports for remote MCP servers. SSE is explicitly supported and natively handled by the `FastMCP` framework.
9. **App submission requirements:** Requires a working OAuth flow (if auth is declared), valid metadata, and a privacy policy URL.
10. **User data privacy and deletion requirements:** Strict data minimization. Apps must not request full transcripts.

## 2. What CogniCore Currently Supports
- **MCP Transport:** The Phase 1 FastAPI wrapper successfully exposes `FastMCP` over an SSE transport (`/mcp/sse`), which is compatible with OpenAI's remote endpoint requirements.
- **Tenant Isolation (Local):** The existing Claude Code plugin successfully manages local JWTs and environment variables, but this is incompatible with ChatGPT's remote OAuth flow.
- **Stub Tools:** 4 MVP tools are registered but currently lack `securitySchemes` definitions.

## 3. What CogniCore is Missing
- **Identity Provider (IdP):** We currently lack an OAuth 2.0 Authorization Server to issue tokens to ChatGPT.
- **Token Validation:** No FastAPI middleware exists yet to validate incoming Bearer tokens from an IdP.
- **Triggering Auth:** The MCP tools do not currently return the `_meta["mcp/www_authenticate"]` error required to prompt the ChatGPT user to log in.

## 4. Incorrect or Unverified Assumptions from Previous Audits
- **INCORRECT:** *Assumption that hashing a Bearer token acts as authentication.* (Corrected: A token must be cryptographically validated against an IdP).
- **INCORRECT:** *Assumption that OpenAI provides the user identity claim.* (Corrected: OpenAI simply proxies the token provided by whatever IdP you configure in the Developer Portal).
- **INCORRECT:** *Assumption that PostgreSQL is required.* (Corrected: Storage choice is independent of OpenAI. SQLite per-tenant is functionally viable).
- **INCORRECT:** *Assumption that `user_id` is a guaranteed field.* (Corrected: The structure of the identity claim depends on the IdP we select).

## 5. Recommended Authentication Architecture
ChatGPT (OAuth Client) -> Developer's Identity Provider (Auth0/Clerk/Custom) -> ChatGPT receives Access Token -> ChatGPT calls FastAPI MCP Server (Bearer Token) -> FastAPI validates Token -> FastAPI extracts `tenant_id`.

## 6. Recommended Tenant Isolation Architecture
Map the validated `tenant_id` (e.g., the `sub` claim from the IdP) directly to an isolated SQLite file: `~/.cognicore/chatgpt/memory_{tenant_id}.db`. SQLite is sufficient for the MVP provided the hosting platform uses persistent volumes.

## 7. Recommended MCP Transport
**SSE (Server-Sent Events)** wrapped in FastAPI. It is officially supported by OpenAI and already functionally proven in our Phase 1 code.

## 8. Security Risks
- Trusting the Bearer token without verifying its signature and audience against the IdP.
- Path traversal vulnerabilities if the `tenant_id` is used directly in SQLite file paths without sanitization (e.g., hashing or regex validation).

## 9. Required Tests
- Test that unauthenticated calls to `oauth2` tools return the correct `mcp/www_authenticate` error.
- Test that valid Bearer tokens correctly map to isolated SQLite databases.
- Test path traversal protections on the `tenant_id`.

## 10. Exact Implementation Steps (When Ready)
1. Select and configure an Identity Provider (IdP).
2. Write FastAPI middleware to validate JWTs from the selected IdP.
3. Update the 4 MVP tools to declare `securitySchemes: ["oauth2"]`.
4. Implement the `mcp/www_authenticate` error response in `FastMCP` for missing tokens.
5. Update `get_backend_for_user` to use the validated `tenant_id`.

---

# RECOMMENDATION

**PHASE 2 READY: No**

**What information is still missing:**
To implement proper authentication and user identity mapping, we must select an Identity Provider (IdP) to act as the OAuth Authorization Server. Because you explicitly instructed me *not* to introduce Auth0, Clerk, Supabase Auth, or invent a custom OAuth flow yet, I cannot write the JWT validation middleware. 

I need an architectural decision on:
1. **Which Identity Provider** we will use for the ChatGPT OAuth integration.
2. Or, whether we should implement a **mock IdP / API Key fallback** specifically for local development and testing before integrating a real IdP.

## Phase 2B Implementation Summary

- **Files Changed**:
  - `cognicore/integrations/auth.py` (Created)
  - `tests/test_chatgpt_auth.py` (Created)
  - `cognicore/integrations/chatgpt.py` (Updated)
- **Authentication Flow**:
  - Intercepts `Context` in each MCP tool.
  - Validates `Authorization: Bearer <JWT>`.
  - Uses `PyJWKClient` to fetch and cache Supabase RS256 public keys from the JWKS endpoint.
  - Validates signature, audience (`authenticated`), issuer, and expiration.
  - Validates that `sub` is a strict UUID.
  - If validation fails, returns an MCP error containing the `_meta["mcp/www_authenticate"]` challenge.
- **Configuration Required**:
  - `SUPABASE_URL`: Required to dynamically derive the issuer and `.well-known/jwks.json` URL.
- **Tests Added**:
  - Extensive unit tests covering missing headers, invalid signatures, expired tokens, incorrect issuer/audience, non-UUID subjects, and path traversal attempts.
- **Test Results**: All tests (including the full existing test suite) pass successfully.
- **Security Decisions**:
  - Adopted `algorithms=["RS256"]` explicitly to prevent algorithm confusion attacks (e.g., forcing HS256).
  - Used strict UUID casting on the `sub` claim to guarantee deterministic and safe database paths (`memory_<uuid>.db`), neutralizing path traversal.
  - `PyJWKClient` caches keys so we don't spam the Supabase API.


## Phase 2B Security Hardening

- **Authorization header validation**: Strict `Bearer <token>` parsing. Missing, malformed, empty, or incorrectly cased schemes are actively rejected without attempting regex/substring fuzzy matching.
- **JWT validation**: Enforces exact claims including `issuer` (from `SUPABASE_URL`), `audience` (`authenticated`), and explicitly passes `algorithms=["RS256"]` to structurally prevent algorithm confusion attacks (like coercing an `HS256` symmetric check).
- **JWKS handling**: `PyJWKClient` is used to dynamically pull JWKS from `.well-known/jwks.json`. Results are cached securely in memory to eliminate repeated network calls. If Supabase fails, or unexpected PyJWKClient exceptions occur, they are abstracted into generic `authentication failed` errors.
- **UUID validation**: The extracted `sub` claim is strictly cast to `uuid.UUID(sub)`. Any malformed or path traversal strings (e.g. `../../../etc/passwd`) immediately trigger validation failure.
- **Tenant isolation**: The validated `sub` UUID string is directly interpolated into `~/.cognicore/chatgpt/memory_<validated_uuid>.db`. Since the UUID format is strictly enforced and driven entirely by the token rather than request context or parameters, cross-tenant database access is impossible.
- **Error handling**: Comprehensive `try-except` chains catch granular JWT errors (expiration, audience mismatch) and convert them to safe RFC 7235 `Bearer error="..."` challenges. Unexpected backend exceptions are caught securely, logged internally without stack-traces, and returned to the client as a generic `authentication failed` error to prevent sensitive leakages.
- **Tests**: `test_chatgpt_auth.py` contains 15 explicit test cases for JWKS caching, strict tenant isolation database verification, token spoofing (HS256), and path traversal. Tests prove that database paths are deterministically isolated and immune to parameter pollution.


# Remote MCP server (Claude connector)

`cognicore/connectors/remote.py` is the hosted MCP server behind the CogniCore Claude connector and the `cognicore-memory` plugin. Users sign in with GitHub, and each GitHub account gets its own memory database.

This page covers sign-in, configuration and deployment. For what the server's 31 tools do, see [the tool reference](mcp-tools.md).

## How sign-in works

The server is its own OAuth 2.1 authorization server (`cognicore/connectors/oauth.py`) and hands the actual sign-in to GitHub:

1. Claude calls the MCP URL without a token and gets `401` with a `WWW-Authenticate` header pointing at `/.well-known/oauth-protected-resource/...`.
2. Claude reads the metadata, registers itself at `/register`, and opens `/authorize` with PKCE.
3. The user sees a consent page, continues to GitHub, and returns to `/oauth/github/callback`.
4. The server reads the user's numeric GitHub ID, issues its own access and refresh tokens with `sub = github:<id>`, and discards the GitHub token.
5. Every MCP call carries the access token; the server maps `sub` to `memory_<sha256(sub)>.db`.

No user database or Supabase project is involved.

## Endpoints

| URL | Purpose |
|---|---|
| `/mcp/stream` | Streamable HTTP transport. Use this as the connector URL. |
| `/mcp/sse` | Legacy SSE transport, still supported by Claude but deprecated. |
| `/health` | Health check for the platform. |

## Environment variables

| Variable | Required | Value |
|---|---|---|
| `COGNICORE_JWT_SECRET` | Yes | Long random string, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Changing it signs everyone out. |
| `GITHUB_CLIENT_ID` | Yes | From the GitHub OAuth App. |
| `GITHUB_CLIENT_SECRET` | Yes | From the GitHub OAuth App. |
| `COGNICORE_PUBLIC_URL` | Yes | Public origin, e.g. `https://mcp.cognicoree.tech`. Must match the GitHub callback URL's origin. |
| `COGNICORE_DATA_DIR` | Set by the Dockerfile | `/data/cognicore`. Memories go to `$COGNICORE_DATA_DIR/remote`, so mount a persistent volume at `/data`. |
| `COGNICORE_USE_SEMANTIC` | No | `0` to skip loading sentence-transformers. |
| `COGNICORE_EXTRA_REDIRECT_URIS` | No | Comma-separated extra OAuth redirect URIs accepted at `/register`, on top of Claude's callbacks and RFC 8252 loopback. Add one per MCP client that needs to register dynamically — e.g. `https://connect.smithery.ai/oauth/callback` for Smithery. |
| `COGNICORE_RATE_LIMIT_USER` | No | Requests per minute per signed-in user. Default `120`; `0` turns it off. |
| `COGNICORE_RATE_LIMIT_IP` | No | Requests per minute per IP address for requests without a valid token (sign-in steps, scans). Default `120`; `0` turns it off. Signed-in users are never limited per IP, because Claude and ChatGPT share IP ranges. |

## GitHub OAuth App

GitHub → Settings → Developer settings → OAuth Apps → New OAuth App:

- **Homepage URL:** `https://mcp.cognicoree.tech`
- **Authorization callback URL:** `https://mcp.cognicoree.tech/oauth/github/callback`

The callback URL must use the same origin as `COGNICORE_PUBLIC_URL`.

## Deploying on a VM (Docker Compose)

Production runs on a single Linux VM with `deploy/docker-compose.yml`: the server plus Caddy, which terminates TLS with an automatic Let's Encrypt certificate and passes the client address in `X-Real-IP`.

1. Attach a data disk and prepare the host (Docker, swap, firewall, disk mounted at `/mnt/cognicore-data`):

   ```bash
   sudo bash deploy/setup-host.sh /dev/sdb
   ```

   SQLite needs a local filesystem; do not put the data directory on SMB or NFS, and turn off host disk caching.
2. Copy `deploy/.env.example` to `deploy/.env`, fill in the variables above, and `chmod 600` it.
3. Point an `A` record for `mcp` at the VM's public IP. Keep the Cloudflare proxy off (DNS only); if you turn it on, Caddy must forward `CF-Connecting-IP` instead, or the rate limiter sees Cloudflare's address.
4. Start it:

   ```bash
   docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
   ```

5. Verify:

   ```bash
   python scripts/checks/verify_remote_mcp.py https://mcp.cognicoree.tech
   ```

6. In Claude, add a custom connector with URL `https://mcp.cognicoree.tech/mcp/stream` and sign in with GitHub.

`railway.toml` is kept for platforms that build from the Dockerfile; set the variables and mount a volume at `/data` there instead.

## Testing locally

`tests/test_remote_oauth.py` covers the OAuth flow with GitHub stubbed out. To run the server locally:

```bash
COGNICORE_JWT_SECRET=dev-secret GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=... \
COGNICORE_PUBLIC_URL=http://localhost:8000 \
uvicorn cognicore.connectors.remote:app --port 8000
```

A local GitHub OAuth App needs the callback `http://localhost:8000/oauth/github/callback`.

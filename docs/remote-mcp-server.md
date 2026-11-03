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
| `PUT /sync/sessions`, `GET /sync/sessions?since=` | Session sync for `cognicore login` on local machines (`cognicore/sync.py`). The same bearer tokens as MCP; a handoff is at most 512 KB, and a newer revision always wins. |

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

## Automatic deploys

Merging to `main` deploys the server. Nobody needs access to the VM.

A systemd timer on the VM runs the deploy script every 3 minutes. When `main`
has a new commit, it:

1. **holds** the commit if it changes anything under `deploy/` other than
   `requirements.in` / `requirements.txt`. Those files set the container's
   confinement and the host's configuration, so a person deploys them (below);
2. reads the workflow runs for that exact commit and continues only if
   `.github/workflows/ci.yml`, run by the push to `main` of this repository,
   **succeeded**. A result from any other workflow, a fork, a pull request or a
   manual run does not count, and other workflows (Pages, dashboards, scanners)
   cannot block a deploy. If CI is still running it waits for the next tick;
3. checks the exact commit out and runs `docker compose up -d --build`;
4. waits up to 4 minutes for the container's health check, then requests
   `https://$COGNICORE_DOMAIN/health`. If either fails it restores the previous
   commit, checks that *it* is healthy, and does not try the failed commit
   again until a newer one lands or someone runs `--retry`.

Git runs as the checkout's owner rather than root, and a deploy stops before
anything restarts if a source file is unreadable to the container's user.

### What this does and does not protect

- The VM pulls. No port is opened for GitHub and no credential is stored in
  GitHub or on the VM; the CI lookup is unauthenticated because the repository
  is public.
- Root runs a copy of the script installed in `/usr/local/sbin`, not the one in
  the checkout, so a commit cannot change what root executes.
- Git runs with hooks and fsmonitor disabled, so the checkout's own config
  cannot make root run anything.
- **Anyone who can merge to `main` can change the code running in the
  container**, which holds every user's memories and the server's secrets. That
  is the remaining trust boundary, and it is protected only by GitHub account
  security: require two-factor authentication for the organization, and protect
  `main` so it accepts only reviewed pull requests whose CI passed.

### Install it (once)

The first deploy runs through the script too, so it gets the same health check
and rollback. Do not `git pull` first: the script would see the checkout at
`main` and report it as already deployed.

```bash
cd /opt/cognicore-env && git fetch origin main
git show origin/main:deploy/auto-deploy.sh > /tmp/cognicore-deploy
sudo install -o root -g root -m 0755 /tmp/cognicore-deploy /usr/local/sbin/cognicore-deploy
for unit in cognicore-deploy.service cognicore-deploy.timer; do
    git show "origin/main:deploy/systemd/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo cognicore-deploy --dry-run --allow-infra   # what it would deploy
sudo cognicore-deploy --allow-infra             # the first deploy
sudo systemctl enable --now cognicore-deploy.timer
```

`--allow-infra` is needed here because the commits being deployed add the
deploy files themselves.

### Deploy a held change

The journal shows which files caused the hold. Read the diff, then deploy it:

```bash
cd /opt/cognicore-env && git fetch origin main
git diff HEAD origin/main -- deploy/
sudo cognicore-deploy --allow-infra
```

If the change is to `deploy/auto-deploy.sh` or `deploy/systemd/`, reinstall
them first with the `git show` lines from "Install it".

### Operate it

| Task | Command |
|---|---|
| Watch deploys | `journalctl -u cognicore-deploy -f` |
| Last result | `systemctl status cognicore-deploy` |
| Next check | `systemctl list-timers cognicore-deploy.timer` |
| Deploy now | `sudo systemctl start cognicore-deploy` |
| Pause deploys | `sudo systemctl stop cognicore-deploy.timer` |
| Resume deploys | `sudo systemctl start cognicore-deploy.timer` |
| Retry a commit whose deploy failed | `sudo cognicore-deploy --retry` |

### If a deploy fails

The journal ends with one of:

- `rolled back to <sha>; the server is running it`: users are unaffected. Find
  the cause with `sudo docker compose -f deploy/docker-compose.yml --env-file deploy/.env logs mcp`
  and merge a fix; the next commit deploys normally.
- `ROLLBACK FAILED - THE SERVER IS DOWN`: stop the timer
  (`sudo systemctl stop cognicore-deploy.timer`), read the same logs, and
  restore by hand.

To withdraw a bad commit that passed CI, revert it on `main`; the revert
deploys like any other commit.

## Testing locally

`tests/test_remote_oauth.py` covers the OAuth flow with GitHub stubbed out. To run the server locally:

```bash
COGNICORE_JWT_SECRET=dev-secret GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=... \
COGNICORE_PUBLIC_URL=http://localhost:8000 \
uvicorn cognicore.connectors.remote:app --port 8000
```

A local GitHub OAuth App needs the callback `http://localhost:8000/oauth/github/callback`.

# CogniCore ChatGPT App Deployment Guide

## 1. Local Architecture (Development)
During local development, the architecture consists of the FastAPI server running natively (or via Docker) mapping tenant data to a local `.cognicore/` folder.

```mermaid
graph TD
    A[ChatGPT OAuth] -->|Bearer Token| B(FastAPI / FastMCP Server)
    B --> C{SQLiteMemoryBackend}
    C -->|memory_userA.db| D[~/.cognicore/chatgpt/]
    C -->|memory_userB.db| D
```

## 2. Production Architecture (Cloud Deployment)
For production deployment, the App must be hosted behind a public HTTPS endpoint with a persistent volume attached to prevent data loss when the stateless container restarts.

```mermaid
graph TD
    A[ChatGPT Clients] -->|HTTPS SSE / POST| B[Cloud Load Balancer / SSL Termination]
    B --> C(Docker Container: FastAPI/MCP)
    C --> D{Persistent Block Volume}
    D -->|/data/cognicore/memory_userA.db| E[(User A SQLite)]
    D -->|/data/cognicore/memory_userB.db| F[(User B SQLite)]
```

### Environment Variables Required
* `SUPABASE_URL`: The URL of your Supabase project (used for JWKS fetching).
* `COGNICORE_DATA_DIR`: Must be set to `/data/cognicore` (or wherever your persistent volume is mounted).

### Health Checks
The container exposes `GET /health` which returns `200 OK {"status": "ok"}` for load balancer readiness probes.

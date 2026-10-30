# Base image pinned by digest so a rebuild cannot silently pick up a changed
# image. Dependabot (docker ecosystem) proposes digest updates as PRs.
FROM python:3.11-slim@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60

WORKDIR /app

# Exact versions with hashes: pip refuses any file whose hash differs, so a
# poisoned or re-published release cannot slip into a build. To change them,
# edit deploy/requirements.in and recompile deploy/requirements.txt.
COPY deploy/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY cognicore/ cognicore/

# Environment configuration. Several modules create scratch files under
# ~/.cognicore on import; the root filesystem is read-only in production, so
# HOME points at the writable /tmp. User data lives in COGNICORE_DATA_DIR.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HOME=/tmp \
    COGNICORE_DATA_DIR=/data/cognicore \
    SUPABASE_URL=""

# Run as an unprivileged user. The data volume must be owned by uid 10001
# (deploy/setup-host.sh does this for the VM deployment).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin cognicore \
    && mkdir -p /data/cognicore \
    && chown -R cognicore /data
USER 10001

EXPOSE 8000

# MCP requires stdout for stdio transport, but we use SSE so standard uvicorn logging is fine
CMD ["uvicorn", "cognicore.connectors.chatgpt:app", "--host", "0.0.0.0", "--port", "8000"]

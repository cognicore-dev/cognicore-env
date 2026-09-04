FROM python:3.11-slim

WORKDIR /app

# Install build dependencies for sqlite/native extensions if any
RUN apt-get update && apt-get install -y --no-install-recommends gcc python3-dev && rm -rf /var/lib/apt/lists/*

# Copy core files
COPY pyproject.toml requirements.txt ./

# Install python dependencies
RUN pip install --no-cache-dir uv && uv pip install --system -e . 
RUN uv pip install --system uvicorn fastapi mcp pyjwt cryptography

# Copy application code
COPY cognicore/ cognicore/

# Environment configuration
ENV PYTHONUNBUFFERED=1
ENV COGNICORE_DATA_DIR=/data/cognicore
ENV SUPABASE_URL=""

# Ensure data directory exists and is accessible
RUN mkdir -p /data/cognicore

EXPOSE 8000

# MCP requires stdout for stdio transport, but we use SSE so standard uvicorn logging is fine
CMD ["uvicorn", "cognicore.integrations.chatgpt:app", "--host", "0.0.0.0", "--port", "8000"]

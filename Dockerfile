# Base image pinned by digest so a rebuild cannot silently pick up a changed
# image. Dependabot (docker ecosystem) proposes digest updates as PRs.
#
# Stage 1: Tesseract, which reads text in images and scanned PDF pages for document memory.
# Installing the package brings about 150 MB of dependencies, most of which Tesseract never
# loads. Only the binary, the English model and the shared libraries it loads that the base
# image lacks are copied into the server image, under /opt/tesseract, with a wrapper that
# points Tesseract (and only Tesseract) at them. Build with --build-arg WITH_OCR=0 to leave
# it out: images are then refused with a message, or described by a vision model if enabled.
FROM python:3.11-slim@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60 AS ocr
ARG WITH_OCR=1
RUN mkdir -p /opt/tesseract && if [ "$WITH_OCR" = "1" ]; then \
        ls /usr/lib/x86_64-linux-gnu > /tmp/base-libs \
        && apt-get update \
        && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
        && mkdir -p /opt/tesseract/bin /opt/tesseract/lib /opt/tesseract/tessdata \
        && cp /usr/bin/tesseract /opt/tesseract/bin/ \
        && cp /usr/share/tesseract-ocr/*/tessdata/eng.traineddata /opt/tesseract/tessdata/ \
        && ldd /usr/bin/tesseract | awk '$3 ~ /^\// {print $3}' | while read -r lib; do \
               grep -qx "$(basename "$lib")" /tmp/base-libs || cp -L "$lib" /opt/tesseract/lib/; \
           done \
        && printf '#!/bin/sh\nLD_LIBRARY_PATH=/opt/tesseract/lib TESSDATA_PREFIX=/opt/tesseract/tessdata exec /opt/tesseract/bin/tesseract "$@"\n' \
               > /opt/tesseract/tesseract \
        && chmod 755 /opt/tesseract/tesseract \
        && /opt/tesseract/tesseract --list-langs | grep -qx eng; \
    fi

# Stage 2: the server.
FROM python:3.11-slim@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60

WORKDIR /app

COPY --from=ocr /opt/tesseract /opt/tesseract

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
    COGNICORE_TESSERACT=/opt/tesseract/tesseract \
    SUPABASE_URL=""

# Run as an unprivileged user. The data volume must be owned by uid 10001
# (deploy/setup-host.sh does this for the VM deployment).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin cognicore \
    && mkdir -p /data/cognicore \
    && chown -R cognicore /data
USER 10001

# The commit this image was built from, reported by /health so a deploy can be
# confirmed from outside. deploy/auto-deploy.sh passes it; other builds say
# "unknown". Declared last so a new commit does not invalidate earlier layers.
ARG COGNICORE_COMMIT=unknown
ENV COGNICORE_COMMIT=$COGNICORE_COMMIT

EXPOSE 8000

# The hosted server: Streamable HTTP at /mcp/stream, SSE at /mcp/sse, so standard
# uvicorn logging is fine. deploy/docker-compose.yml passes the proxy flags on top.
CMD ["uvicorn", "cognicore.connectors.remote:app", "--host", "0.0.0.0", "--port", "8000"]

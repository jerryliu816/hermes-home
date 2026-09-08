# Build stage: resolve dependencies into a venv we can copy across.
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies first, so a source-only change does not re-resolve them.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[anthropic,mcp]"

# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# sqlite3 is needed by the entrypoint's backup step, curl by the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root. The uid is fixed so the bind-mounted ./data directory can be given
# matching ownership on the host if the platform requires it.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin hermes

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/hermes-home.db \
    BACKUP_DIR=/data/backups

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY alembic.ini ./
COPY src/ ./src/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# /data is the only writable location, and it is always a mounted volume.
# Nothing valuable lives in the container filesystem.
RUN mkdir -p /data && chown -R hermes:hermes /data /app
VOLUME ["/data"]

USER hermes
EXPOSE 8099

# Liveness only: deliberately does not test Home Assistant or the vision
# provider, because restarting on a dependency outage would be wrong.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8099/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# --no-access-log: the app already logs each webhook with its client address,
# and the 30s healthcheck would otherwise dominate the log.
CMD ["uvicorn", "hermes_home.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8099", "--no-access-log", \
     "--timeout-graceful-shutdown", "40"]

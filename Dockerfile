# syntax=docker/dockerfile:1.6
# muselab-codex: FastAPI + the pinned Codex app-server CLI.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /bin/
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim

ARG CODEX_CLI_VERSION=0.144.1
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git nodejs npm && \
    npm install -g "@openai/codex@${CODEX_CLI_VERSION}" && \
    apt-get clean && rm -rf /var/lib/apt/lists/* /root/.npm /tmp/*

COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /usr/local/bin/
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY backend ./backend
COPY frontend ./frontend
COPY pyproject.toml ./

RUN groupadd -g 1000 muse && \
    useradd -u 1000 -g 1000 -m -s /bin/bash muse && \
    mkdir -p /data /home/muse/.codex && \
    chown -R muse:muse /app /data /home/muse/.codex

USER muse
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MUSELAB_PORT=8765 \
    MUSELAB_ROOT=/data \
    CODEX_HOME=/home/muse/.codex

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS --max-time 3 http://127.0.0.1:8765/api/health >/dev/null || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8765"]

# syntax=docker/dockerfile:1
# One image: FastAPI backend (+ LibreDWG) serving the built React SPA. Build context = repo root.

# ---- Stage 1: build LibreDWG (provides `dwg2dxf` for DWG -> DXF conversion) ----------------
FROM python:3.12-slim-trixie AS libredwg
ARG LIBREDWG_VERSION=0.14
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates curl xz-utils pkg-config \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL "https://ftp.gnu.org/gnu/libredwg/libredwg-${LIBREDWG_VERSION}.tar.xz" | tar -xJ --strip-components=1
RUN ./configure --prefix=/opt/libredwg --disable-bindings --disable-static \
 && make -j"$(nproc)" \
 && make install-strip

# ---- Stage 2: build the frontend (static files incl. /furniture/*.glb) ------------------------
FROM node:22-alpine AS frontend
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Stage 3: runtime --------------------------------------------------------------------------
FROM python:3.12-slim-trixie AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv
COPY --from=libredwg /opt/libredwg /opt/libredwg

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:/opt/libredwg/bin:${PATH}" \
    LD_LIBRARY_PATH=/opt/libredwg/lib \
    STATIC_DIR=/app/static

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
COPY --from=frontend /web/dist /app/static

RUN useradd --create-home --uid 1000 app && dwg2dxf --version
USER app

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
# Migrate, seed (idempotent; never blocks startup), then serve. Only the reverse proxy can reach
# the container, so forwarded headers from any peer are trusted.
CMD ["sh", "-c", "alembic upgrade head && (python scripts/seed.py || echo 'Seed failed; continuing') && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]

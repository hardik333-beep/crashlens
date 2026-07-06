# Single-container image: the whole product (API + background-free static SPA)
# in one container, for PaaS platforms that run one image per service and put
# their own TLS/edge in front (Railway, Fly, Render). The compose stack does NOT
# use this file; it keeps Caddy + separate api/dashboard containers. Here uvicorn
# serves BOTH the FastAPI API and the compiled dashboard, and strips the /api
# prefix itself (see SERVE_DASHBOARD_DIR handling in server/app/main.py).
#
# The background worker is still its own service/container in this mode: run
# deploy/worker.Dockerfile alongside this image, pointed at the same Postgres and
# Redis. This image is the web (HTTP) half only.
#
# Build context is the repository root.

# --- Stage 1: compile the Vite dashboard to static files ---------------------
FROM node:22-alpine AS dashboard
WORKDIR /dashboard

# Install dependencies against the lockfile first for layer caching.
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci

# Build the production bundle (package.json "build" runs tsc --noEmit && vite build).
COPY dashboard/ ./
RUN npm run build

# --- Stage 2: the Python API image that also serves the built dashboard ------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the server package and its runtime dependencies.
COPY server/ /app/
RUN pip install --upgrade pip && pip install .

# Carry the compiled dashboard in and tell the app to serve it. With
# SERVE_DASHBOARD_DIR set, the API process mounts the SPA and answers /api/*
# itself, so no reverse proxy is needed in front of this container.
COPY --from=dashboard /dashboard/dist /srv/dashboard
ENV SERVE_DASHBOARD_DIR=/srv/dashboard

EXPOSE 8000

# PaaS platforms inject $PORT; default to 8000 for a plain `docker run`.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

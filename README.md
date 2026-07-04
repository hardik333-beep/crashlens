# Crashlens

Crashlens is an open source, self-hosted error monitoring system: you add a small SDK to your app, and unhandled exceptions are captured, grouped into issues, and shown in a dashboard with stack traces, breadcrumbs, and occurrence trends so your team can find and fix crashes fast. It is designed to run entirely on your own infrastructure with a single command and no hosted dependency.

## Quickstart

> Placeholder. The full quickstart lands with the ingest and dashboard slices. The intended shape is:
>
> ```bash
> cp .env.example .env   # then edit the placeholder values
> docker compose up
> ```
>
> This will bring up the reverse proxy, API, background worker, Postgres, and Redis. Point your browser at the proxy and the dashboard will load.

## Repository layout

- `server/` - FastAPI application (API + background worker), Alembic migrations, tests.
- `dashboard/` - Vite + React + TypeScript dashboard, served as static files in production.
- `sdks/python/`, `sdks/browser/`, `sdks/node/` - client SDKs (stubs for now).
- `docs/` - protocol and design documentation. See `docs/PROTOCOL.md` (DRAFT).
- `deploy/` - `Caddyfile` and the per-service Dockerfiles referenced by `docker-compose.yml`.
- `scripts/` - operational helper scripts.

The `docker-compose.yml` and `.env.example` at the repository root are the self-host entry point.

## License

MIT. See [LICENSE](LICENSE).

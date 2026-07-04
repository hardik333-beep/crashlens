# Configuration reference

Crashlens is configured entirely through environment variables, read by the
API and worker from `.env` (see `server/app/config.py`). Copy
`.env.example` to `.env` and edit it; `docker-compose.yml` loads `.env` into
both the `api` and `worker` containers via `env_file`.

Postgres and Redis themselves are also configured through this same `.env`
file (`docker-compose.yml` reads `POSTGRES_USER`, `POSTGRES_PASSWORD`, and
`POSTGRES_DB` directly for the `postgres` service).

## Required

The application refuses to start if any of these are missing; there is no
silent fallback.

| Variable | Type | Effect |
| --- | --- | --- |
| `DATABASE_URL` | string (asyncpg URL) | Postgres connection string used by the API and worker, for example `postgresql+asyncpg://crashlens:PASSWORD@postgres:5432/crashlens`. Must use the `postgresql+asyncpg://` scheme. |
| `REDIS_URL` | string (redis URL) | Redis connection used for the ingest job queue and per-DSN rate limiting, for example `redis://redis:6379/0`. |
| `SECRET_KEY` | string | Signs session tokens. Generate a long random value, for example with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Rotating it invalidates every existing session. |

## Postgres bootstrap (read by `docker-compose.yml`, not by the application)

| Variable | Type | Effect |
| --- | --- | --- |
| `POSTGRES_USER` | string | Login role the `postgres` container creates on first boot, and the role `DATABASE_URL` must connect as. Keep this in sync with the username inside `DATABASE_URL`. |
| `POSTGRES_PASSWORD` | string | Password for `POSTGRES_USER`. Keep this in sync with the password inside `DATABASE_URL`. |
| `POSTGRES_DB` | string | Database name created on first boot. Keep this in sync with the database name inside `DATABASE_URL`. |

## Optional

| Variable | Type | Default | Effect |
| --- | --- | --- | --- |
| `ENVIRONMENT` | string | `development` | A label for the deployment environment. Set to `production` for a real deployment. |
| `SOURCEMAPS_DIR` | string (filesystem path) | `/var/lib/crashlens/sourcemaps` | Directory where uploaded JavaScript source maps are stored, keyed by org/project/release. The default matches where `docker-compose.yml` mounts the `sourcemaps` volume in both the `api` and `worker` containers; only change this if you also change the volume mount. Not present in `.env.example` because the default is almost always correct. |
| `PUBLIC_BASE_URL` | string (URL, no trailing slash) | unset | When set (for example `https://crashlens.example.com`), it is prefixed to the issue link inside alert emails, Slack messages, and webhooks so they are clickable. When unset, alerts carry only the relative `/org/.../issues/...` path. |

## Email alerts (optional block)

Email alerting is off unless **both** `SMTP_HOST` and `SMTP_FROM` are set.
When either is missing, the alert engine logs a single warning once per
process and skips email channels only; Slack and generic webhook alert
channels work regardless of this block.

| Variable | Type | Default | Effect |
| --- | --- | --- | --- |
| `SMTP_HOST` | string | unset | SMTP server hostname. Required (with `SMTP_FROM`) to enable email alerts. |
| `SMTP_PORT` | integer | `587` | SMTP server port. |
| `SMTP_USERNAME` | string | unset | SMTP auth username. May be omitted for a relay that needs no authentication (for example an internal MTA on localhost). |
| `SMTP_PASSWORD` | string | unset | SMTP auth password. May be omitted alongside `SMTP_USERNAME`. |
| `SMTP_FROM` | string (email address) | unset | The `From` address on alert emails. Required (with `SMTP_HOST`) to enable email alerts. |
| `SMTP_STARTTLS` | boolean | `true` | Whether to negotiate STARTTLS with the SMTP server. |

## What is not an environment variable

Per-project **sampling rate** (what fraction of incoming events a project
keeps) is not configured through `.env` at all: it is set per project from
the project settings page in the dashboard, and stored as `sampling_rate` on
the project row. There is no global sampling env var to set.

Per-project **retention** (`retention_days` on the project row) currently
defaults to 30 days at creation and is not yet exposed in the dashboard to
change; changing it today requires a direct database update.

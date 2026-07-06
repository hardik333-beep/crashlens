# Self-hosting Crashlens

This is the step-by-step guide to running Crashlens on your own server. It
assumes the [quickstart in the root README](../README.md#quickstart) is your
starting point and goes one level deeper on each step.

## Requirements

- A machine with Docker and the Docker Compose plugin installed. A small VPS
  or your own laptop both work for a trial; see
  [Resource expectations](#resource-expectations) below.
- A domain name pointed at the machine, if you want a real HTTPS deployment.
  Not required for a local trial on `localhost`.
- Ports 80 and 443 reachable from wherever your team and your instrumented
  apps are. Caddy binds both (see `docker-compose.yml`).

## First run

### 1. Get the code and configure your environment

```bash
git clone https://github.com/hardik333-beep/crashlens.git
cd crashlens
cp .env.example .env
```

Open `.env` and replace every `REPLACE_WITH_...` placeholder. There are two
database passwords, and each must agree with the connection URL that carries
it:

- `CRASHLENS_DB_APP_PASSWORD` is the runtime (application) password; keep it
  in sync with the password inside `DATABASE_URL`.
- `POSTGRES_PASSWORD` is the superuser password; keep it in sync with the
  password inside `MIGRATIONS_DATABASE_URL`.

You also need a random `SECRET_KEY`. The comment in `.env.example` shows how
to generate one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Every variable is documented in [configuration.md](configuration.md).

### 2. Create the database schema

**How the two database users work.** The stack deliberately uses two
Postgres users. `POSTGRES_USER` is the superuser the postgres container
creates at first init: it owns the schema and is used for migrations only
(DDL needs ownership), and the application never connects as it.
`crashlens_login` is a non-superuser runtime user that the API and worker
connect as via `DATABASE_URL`; because it is not a superuser, PostgreSQL Row
Level Security actually constrains it, which is what makes tenant isolation
real at the database layer. The `crashlens_login` role, its password
(`CRASHLENS_DB_APP_PASSWORD`), and its membership in the privilege roles the
migrations expect are all created automatically at first cluster init by
`deploy/postgres-init/01-app-user.sh`; there is no manual role or grant step.

Crashlens does not run migrations automatically when the API container
starts (the `api` image's entrypoint is a plain `uvicorn` process, nothing
else). Run them yourself, once, before the first `docker compose up`. The
migration command overrides `DATABASE_URL` with `MIGRATIONS_DATABASE_URL`
(the superuser connection), and that variable comes from YOUR shell, not
from Compose: `docker compose` reads `.env` to interpolate the compose file,
but it does not export anything into the shell you type commands in. So
load `.env` into the shell first:

```bash
set -a; . ./.env; set +a
docker compose run --rm -e DATABASE_URL=${MIGRATIONS_DATABASE_URL} api alembic upgrade head
```

This works without the rest of the stack running, other than Postgres:
Compose will start `postgres` first because `api` depends on it.

`alembic.ini` sets no database URL itself; it reads `DATABASE_URL` from the
environment through the application's own settings, which is why the one-off
`-e DATABASE_URL=...` override is the entire mechanism for running
migrations as the schema owner.

> **Note on tenant isolation.** PostgreSQL superusers bypass Row Level
> Security entirely by design (documented in migration `0001`'s own notes),
> which is exactly why the default setup already splits the two users for
> you: the app runs as the non-superuser `crashlens_login`, so RLS is
> enforced out of the box. Keep it that way. Do not point `DATABASE_URL` at
> the superuser, even to "fix" a permissions error; that would silently
> disable tenant isolation.

### 3. Bring the stack up

```bash
docker compose up -d
```

Six services start: `caddy`, `dashboard` (a one-shot build that publishes
the compiled dashboard into a shared volume, then exits, this is expected
and not a crash), `api`, `worker`, `postgres`, and `redis`.

Visit the address you are serving from (`http://localhost` for a local
trial, or your domain over HTTPS once step 4 is done). The first thing you
will see is the signup page.

### 4. Sign up

Signing up creates a new user, a new organization named after whatever you
enter, and makes you that organization's admin, all in one transaction.

The very first person to sign up on a fresh instance (an empty `users`
table) is additionally promoted to instance administrator, an instance-wide
flag separate from any organization's admin/member role, so a self-hoster is
never locked out of instance-level administration on their own install.
Every signup after that first one is a plain user with no instance-admin
flag, each getting their own new organization as usual. If you ever need to
grant instance-admin to a different account later (for example after
removing the original one), there is a recovery command:

```bash
docker compose exec api python -m app.cli make-admin someone@example.com
```

From the organization overview, create a project, then create a DSN key for
it. The project page shows an install snippet with the ingest endpoint and
the key: follow the link to the SDK for your platform
([docs/sdks.md](sdks.md)) and paste them in.

## HTTPS

Caddy handles TLS automatically through the `CRASHLENS_SITE_ADDRESS`
environment variable, read directly by `deploy/Caddyfile`:

```bash
export CRASHLENS_SITE_ADDRESS=crashlens.example.com
docker compose up -d
```

Left unset, Caddy falls back to `:80` (plain HTTP), which is fine for local
development but not for a real deployment: your session token and DSN keys
would otherwise travel in the clear. Point the domain's DNS at your server
before starting the stack so Caddy's automatic certificate issuance can
succeed.

## Single-container mode (PaaS: Railway/Fly/Render)

The `docker compose` stack above is the recommended way to self-host: it runs
Caddy, the API, the worker, Postgres, and Redis together on one machine. Some
platforms, though, run one image per service and provide their own TLS/edge and
managed Postgres/Redis (Railway, Fly.io, Render). For those, Crashlens ships a
single-container image, `deploy/railway.Dockerfile`, that folds the dashboard
and the API into one web container: uvicorn serves the compiled dashboard AND
answers the browser's `/api/*` calls itself, so no Caddy is needed in front.

That image is the **web** half only. The background **worker** is still its own
container: build `deploy/worker.Dockerfile` as a second service pointed at the
same Postgres and Redis. Migrations are still run once, by hand, as the
schema-owning superuser, exactly as in [step 2](#2-create-the-database-schema)
above; there is no automatic-migration-on-boot.

What makes it work is one environment variable, `SERVE_DASHBOARD_DIR`. When it
points at a compiled dashboard build (the Railway image bakes the build in at
`/srv/dashboard` and sets the variable for you), the API process mounts the SPA
and strips the `/api` prefix in-process, the one job Caddy does in the compose
stack. Leave `SERVE_DASHBOARD_DIR` unset and the app behaves exactly as it does
behind Caddy: no static files, no `/api` handling, just the API.

Required environment on the web service: `DATABASE_URL` (the non-superuser
`crashlens_login` connection, `postgresql+asyncpg://...`), `REDIS_URL`, and
`SECRET_KEY`. Set `PUBLIC_BASE_URL` to the service's public URL so alert links
are absolute. The worker service takes the same variables except
`PUBLIC_BASE_URL`. The platform's injected `$PORT` is honored automatically.

Because the platform terminates TLS at its edge, keep `DATABASE_URL` pointed at
the non-superuser role here too: the tenant-isolation note in
[step 2](#2-create-the-database-schema) applies unchanged.

## Updating to a new version

```bash
# 1. Back up first. See backup-restore.md.
./scripts/backup.sh

# 2. Pull the new code.
git pull

# 3. Apply any new migrations BEFORE restarting the application containers,
#    so the schema a fresh api/worker process expects already exists.
#    Migrations run as the schema-owning superuser; load .env into the shell
#    so ${MIGRATIONS_DATABASE_URL} expands.
set -a; . ./.env; set +a
docker compose run --rm -e DATABASE_URL=${MIGRATIONS_DATABASE_URL} api alembic upgrade head

# 4. Rebuild and restart.
docker compose up -d --build
```

See [upgrading.md](upgrading.md) for the migration reversibility guarantee
and what to do if you ever need to roll a release back.

## Where your data lives

Everything Crashlens writes lives in named Docker volumes, declared at the
bottom of `docker-compose.yml`:

- `pg_data` - the Postgres data directory: every organization, project,
  issue, and event.
- `sourcemaps` - uploaded JavaScript source maps, laid out by
  org/project/release, mounted into both the `api` and `worker` containers
  at the path `SOURCEMAPS_DIR` points to.
- `dashboard_dist` - the compiled dashboard static assets, rebuilt each time
  the `dashboard` one-shot service runs.
- `caddy_data`, `caddy_config` - Caddy's TLS certificates and internal
  state.

Only `pg_data` and `sourcemaps` hold data you cannot regenerate by rebuilding
or re-issuing a certificate; those are the two volumes
[backup-restore.md](backup-restore.md) covers.

## Resource expectations

Crashlens is designed to run comfortably on a single small VPS: everything
in `docker-compose.yml` is one Postgres instance, one Redis instance, and two
lightweight Python processes behind Caddy, no separate services to
provision. Actual capacity depends entirely on your error volume and
retention settings, which this repository has not load-tested against
specific hardware, so no throughput or sizing numbers are given here.
Watch disk usage on the `pg_data` volume as your event history grows; the
daily partitioning and per-project retention described in
[configuration.md](configuration.md) exist specifically to bound that
growth.

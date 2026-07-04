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

Open `.env` and replace every `REPLACE_WITH_...` placeholder. At minimum you
must set a real `POSTGRES_PASSWORD` (and keep it in sync with the password
inside `DATABASE_URL`, since both variables have to agree) and a random
`SECRET_KEY`. The comment in `.env.example` shows how to generate one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Every variable is documented in [configuration.md](configuration.md).

### 2. Create the database schema

Crashlens does not run migrations automatically when the API container
starts (the `api` image's entrypoint is a plain `uvicorn` process, nothing
else). Run them yourself, once, before the first `docker compose up`, using
a one-off container built from the same image:

```bash
docker compose run --rm api alembic upgrade head
```

This works without the rest of the stack running, other than Postgres:
Compose will start `postgres` first because `api` depends on it.

`alembic.ini` sets no database URL itself; it reads `DATABASE_URL` from the
environment through the application's own settings, so this command uses
whatever you put in `.env`.

**Grant the application roles.** Migration `0001` creates two PostgreSQL
roles that Row Level Security depends on (`crashlens_app`, a bound
read/write role, and `crashlens_system`, a narrow read-only bootstrap role),
but a `CREATE ROLE` cannot grant itself membership to the login user your
`DATABASE_URL` connects as. Do that once, right after the migration:

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "GRANT crashlens_app TO $POSTGRES_USER; GRANT crashlens_system TO $POSTGRES_USER;"
```

`docker compose run --rm api ...` in the previous step starts `postgres` as
a normal background service (because `api` depends on it), so it is already
running and reachable with `exec` by this point; substitute your actual
`.env` values for `$POSTGRES_USER` / `$POSTGRES_DB`, or run
`export $(grep -v '^#' .env | xargs)` first so the shell picks them up.

> **Note on tenant isolation.** The `POSTGRES_USER` the compose file
> provisions is created by the official Postgres image as its bootstrap
> superuser, and PostgreSQL superusers bypass Row Level Security entirely by
> design (this is documented directly in migration `0001`'s own notes). For
> a quick trial this is harmless. If you are running Crashlens for a real
> team and want the database itself to enforce tenant isolation rather than
> relying only on the application code, create a dedicated non-superuser
> login role, grant it `crashlens_app` and `crashlens_system` as above, and
> point `DATABASE_URL` at that role instead of the bootstrap superuser.

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

## Updating to a new version

```bash
# 1. Back up first. See backup-restore.md.
./scripts/backup.sh

# 2. Pull the new code.
git pull

# 3. Apply any new migrations BEFORE restarting the application containers,
#    so the schema a fresh api/worker process expects already exists.
docker compose run --rm api alembic upgrade head

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

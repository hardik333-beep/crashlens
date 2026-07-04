# Upgrading

## The migration promise

Every Alembic revision in `server/alembic/versions/` ships both an explicit
`upgrade()` and an explicit `downgrade()`, written by hand (autogenerate is
not used). That is a hard rule for this project, not a convention some
revisions happen to follow: it means you can always move forward to a new
schema, and, with one documented exception below, always move back.

**The exception:** revision `0004_comment_author_nullable` makes
`issue_comments.author` nullable so that deleting a user (which sets their
authored comments' `author` to `NULL` via the foreign key) does not violate
a `NOT NULL` constraint. Its `downgrade()` restores `NOT NULL`, but any
comment whose author was deleted in the meantime already holds a `NULL`
author with no honest value to backfill, so the downgrade deletes those
orphaned comments before restoring the constraint. If you ever downgrade
past revision `0004`, read that migration's docstring first: it explains
exactly why deletion is the only coherent reversal and that those rows are
unrecoverable afterward. Every other revision's downgrade is fully
non-destructive.

## Safe upgrade order

Always in this order, never restart the application containers before the
migration has run against the new schema they expect:

```bash
# 1. Back up. See backup-restore.md. Do this every time, not just for
#    "big" releases -- the cost of skipping it once is the whole database.
./scripts/backup.sh

# 2. Pull the new code.
git pull

# 3. Migrate BEFORE restarting anything. A fresh api/worker process reads
#    from a schema it expects to already exist. Migrations run as the
#    schema-owning superuser (MIGRATIONS_DATABASE_URL), and that variable
#    must come from YOUR shell (docker compose reads .env only to fill in
#    the compose file, not your command line), so load .env first.
set -a; . ./.env; set +a
docker compose run --rm -e DATABASE_URL=${MIGRATIONS_DATABASE_URL} api alembic upgrade head

# 4. Rebuild the images (the dashboard and server images both changed) and
#    restart.
docker compose up -d --build
```

If step 3 fails, stop: do not proceed to step 4. Restore from the backup
you just took in step 1 and investigate before trying again.

## Downgrading

If a release needs to be rolled back:

```bash
docker compose down
git checkout <previous-tag-or-commit>
set -a; . ./.env; set +a
docker compose run --rm -e DATABASE_URL=${MIGRATIONS_DATABASE_URL} api alembic downgrade <previous-revision-id>
docker compose up -d --build
```

Check the docstring of every migration between your current revision and the
target before downgrading past it; as above, only `0004` is documented as
lossy, but reading each one costs a minute and downgrading a database is not
a decision to make on autopilot.

## Upgrading a cluster that predates the two-user split

If your Postgres data volume was created before the stack shipped the
non-superuser `crashlens_login` runtime role, the init script that normally
creates it (`deploy/postgres-init/01-app-user.sh`) never ran for you: the
postgres image only executes init scripts at first cluster initialisation,
never on a restart. The header comment of that script contains the exact
one-time `psql` command to run as the superuser to create the role and its
grants on an existing cluster; run it once, then point `DATABASE_URL` at
`crashlens_login` (with `CRASHLENS_DB_APP_PASSWORD`) as `.env.example` now
shows. Fresh installs need none of this.

## What does not need a migration

Not every version bump ships a schema change. Skip step 3 above (there is
nothing to migrate) but always still pull, rebuild, and restart in that
order.

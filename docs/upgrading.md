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
#    from a schema it expects to already exist.
docker compose run --rm api alembic upgrade head

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
docker compose run --rm api alembic downgrade <previous-revision-id>
docker compose up -d --build
```

Check the docstring of every migration between your current revision and the
target before downgrading past it; as above, only `0004` is documented as
lossy, but reading each one costs a minute and downgrading a database is not
a decision to make on autopilot.

## What does not need a migration

Not every version bump ships a schema change. Skip step 3 above (there is
nothing to migrate) but always still pull, rebuild, and restart in that
order.

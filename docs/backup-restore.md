# Backup and restore

Crashlens keeps everything that matters in two places: the Postgres database
(organizations, projects, issues, events, everything relational) and the
`sourcemaps` volume (uploaded JavaScript source maps). Back up both. The
`caddy_data` / `caddy_config` volumes only hold reissuable TLS state and the
`dashboard_dist` volume only holds a rebuildable static asset bundle, so
neither needs a backup of its own.

## Backing up

Run from the repository root, with `.env` in place:

```bash
./scripts/backup.sh
```

This:

1. Reads `POSTGRES_USER` / `POSTGRES_DB` (and `SOURCEMAPS_DIR`, if you set
   it) from your `.env`, the same file `docker-compose.yml` uses, so nothing
   is hardcoded or drifts out of sync with your actual configuration.
2. Runs `pg_dump` inside the running `postgres` container
   (`docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom`)
   and writes the dump to `./backups/crashlens-db-<timestamp>.dump`.
3. Archives the source maps volume by running a throwaway `alpine` container
   that shares the running `api` container's mounts
   (`docker run --rm --volumes-from <api container> alpine tar czf - ...`)
   to `./backups/crashlens-sourcemaps-<timestamp>.tar.gz`.

Pass a destination directory as the first argument
(`./scripts/backup.sh /mnt/backups`) to write somewhere other than
`./backups`. Copy the resulting files off the host: a backup that lives on
the same disk as the instance it backs up does not protect you from a disk
failure.

The database dump requires the `api` service to be running to pick up its
source maps volume; if `api` is not up, the script still writes the database
dump and tells you to start `api` and re-run for the source maps half.

## Restoring

**Stop and read this first: restoring replaces the current database
contents (and, if you pass a source maps archive, the current source maps)
with the backup's. This is destructive.** Do not run it against a live
instance unless you mean to roll it back to the backup's point in time.

```bash
./scripts/restore.sh ./backups/crashlens-db-<timestamp>.dump \
                     ./backups/crashlens-sourcemaps-<timestamp>.tar.gz
```

The source maps archive is optional; omit it to restore only the database.

The script:

1. Prompts for a typed `yes` confirmation (skip with `-y` for non-interactive
   use, for example a scheduled restore drill).
2. **Stops `caddy`, `worker`, and `api` first**, so nothing writes to the
   database or the source maps volume mid-restore. `postgres` and `redis`
   are left running (and started if needed) because the restore itself
   needs a live Postgres to restore into.
3. Runs `pg_restore --clean --if-exists` inside the `postgres` container,
   which drops and recreates every object the dump contains before loading
   its data.
4. If a source maps archive was given, clears the source maps volume and
   extracts the archive into it via the same `--volumes-from` technique the
   backup uses.
5. Brings the full stack back up with `docker compose up -d`.

## Testing your backups

A backup you have never restored is a hope, not a plan. Periodically restore
a backup into a scratch copy of the stack (a separate `docker compose`
project, or a throwaway VM) and confirm you can sign in and see your
projects and issues. Rehearsing the restore is what makes the backup real.

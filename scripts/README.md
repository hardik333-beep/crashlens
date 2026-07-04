# scripts

Operational helper scripts for a self-hosted Crashlens instance. Run from
the repository root (they resolve their own paths, but assume
`docker-compose.yml` and `.env` are one directory up from this folder).

- `backup.sh` - dumps the Postgres database and archives the source maps
  volume to a local directory (`./backups` by default).
- `restore.sh` - restores a database dump (and, optionally, a source maps
  archive) produced by `backup.sh`. Stops the application containers first
  and requires a typed confirmation before it touches anything, since it
  replaces the current data.

See [docs/backup-restore.md](../docs/backup-restore.md) for the full
procedure, including what to back up, how to test a restore, and what each
script does step by step.

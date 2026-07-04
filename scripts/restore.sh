#!/usr/bin/env bash
# Restore a self-hosted Crashlens instance from a backup made by backup.sh.
# Run from the repository root, next to docker-compose.yml and .env.
#
# THIS IS DESTRUCTIVE: it replaces the current database contents (and, if you
# pass a source maps archive, the current source maps) with the backup's.
# Stop and think before running it against a live instance with data you
# have not backed up separately.
#
# Usage:
#   ./scripts/restore.sh [-y] <db-dump-file> [sourcemaps-archive]
#
#   -y   skip the confirmation prompt (for non-interactive use).
set -euo pipefail

assume_yes=0
if [ "${1:-}" = "-y" ]; then
  assume_yes=1
  shift
fi

db_dump="${1:-}"
sourcemaps_archive="${2:-}"

if [ -z "${db_dump}" ]; then
  echo "usage: $(basename "$0") [-y] <db-dump-file> [sourcemaps-archive]" >&2
  exit 1
fi
if [ ! -f "${db_dump}" ]; then
  echo "restore: database dump not found: ${db_dump}" >&2
  exit 1
fi
if [ -n "${sourcemaps_archive}" ] && [ ! -f "${sourcemaps_archive}" ]; then
  echo "restore: source maps archive not found: ${sourcemaps_archive}" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
cd "${repo_root}"

env_file="${repo_root}/.env"
if [ ! -f "${env_file}" ]; then
  echo "restore: ${env_file} not found. Copy .env.example to .env first." >&2
  exit 1
fi

set -a
# shellcheck source=/dev/null
source "${env_file}"
set +a

: "${POSTGRES_USER:?POSTGRES_USER is not set in .env}"
: "${POSTGRES_DB:?POSTGRES_DB is not set in .env}"
sourcemaps_dir="${SOURCEMAPS_DIR:-/var/lib/crashlens/sourcemaps}"

if [ "${assume_yes}" -ne 1 ]; then
  echo "This will STOP api/worker/caddy and REPLACE the '${POSTGRES_DB}' database"
  echo "with ${db_dump}."
  if [ -n "${sourcemaps_archive}" ]; then
    echo "It will also REPLACE the contents of ${sourcemaps_dir} with ${sourcemaps_archive}."
  fi
  read -r -p "Type 'yes' to continue: " confirmation
  if [ "${confirmation}" != "yes" ]; then
    echo "restore: aborted."
    exit 1
  fi
fi

echo "restore: stopping api, worker, and caddy (postgres and redis stay up for the restore)."
docker compose stop caddy worker api

echo "restore: making sure postgres is up."
docker compose up -d postgres
# Wait for the healthcheck docker-compose.yml already defines for postgres,
# via plain "docker inspect" (docker compose ps has no stable Go-template
# support for health status across versions, so this reads it directly).
postgres_container="$(docker compose ps -q postgres)"
until [ "$(docker inspect -f '{{.State.Health.Status}}' "${postgres_container}" 2>/dev/null)" = "healthy" ]; do
  sleep 1
done

echo "restore: restoring database from ${db_dump}"
docker compose exec -T postgres \
  pg_restore -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --clean --if-exists \
  < "${db_dump}"

if [ -n "${sourcemaps_archive}" ]; then
  api_container="$(docker compose ps -a -q api)"
  if [ -z "${api_container}" ]; then
    echo "restore: no 'api' container found, cannot locate the source maps volume." >&2
    echo "restore: run 'docker compose up -d api' once, stop it, and re-run this restore." >&2
  else
    echo "restore: restoring source maps from ${sourcemaps_archive}"
    docker run --rm --volumes-from "${api_container}" alpine \
      sh -c "rm -rf ${sourcemaps_dir:?}/* && tar xzf - -C ${sourcemaps_dir}" \
      < "${sourcemaps_archive}"
  fi
fi

echo "restore: bringing the full stack back up."
docker compose up -d

echo "restore: done."

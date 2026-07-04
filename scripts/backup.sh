#!/usr/bin/env bash
# Back up a self-hosted Crashlens instance: a Postgres dump plus the
# uploaded source maps volume. Run from the repository root, next to
# docker-compose.yml and .env.
#
# Usage:
#   ./scripts/backup.sh [destination-dir]
#
# destination-dir defaults to ./backups (created if missing).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
cd "${repo_root}"

env_file="${repo_root}/.env"
if [ ! -f "${env_file}" ]; then
  echo "backup: ${env_file} not found. Copy .env.example to .env first." >&2
  exit 1
fi

# Load POSTGRES_USER / POSTGRES_DB (and anything else in .env) from the same
# file docker-compose.yml uses, rather than hardcoding credentials here.
set -a
# shellcheck source=/dev/null
source "${env_file}"
set +a

: "${POSTGRES_USER:?POSTGRES_USER is not set in .env}"
: "${POSTGRES_DB:?POSTGRES_DB is not set in .env}"

dest_dir="${1:-${repo_root}/backups}"
mkdir -p "${dest_dir}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
db_dump="${dest_dir}/crashlens-db-${stamp}.dump"
sourcemaps_archive="${dest_dir}/crashlens-sourcemaps-${stamp}.tar.gz"

echo "backup: dumping database '${POSTGRES_DB}' to ${db_dump}"
docker compose exec -T postgres \
  pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --format=custom \
  > "${db_dump}"

api_container="$(docker compose ps -q api)"
if [ -z "${api_container}" ]; then
  echo "backup: the 'api' service is not running, skipping the source maps archive." >&2
  echo "backup: start it with 'docker compose up -d api' and re-run to include source maps." >&2
else
  sourcemaps_dir="${SOURCEMAPS_DIR:-/var/lib/crashlens/sourcemaps}"
  echo "backup: archiving source maps (${sourcemaps_dir}) to ${sourcemaps_archive}"
  docker run --rm --volumes-from "${api_container}" alpine \
    tar czf - -C "${sourcemaps_dir}" . \
    > "${sourcemaps_archive}"
fi

echo "backup: done."
echo "backup: database dump      -> ${db_dump}"
if [ -f "${sourcemaps_archive}" ]; then
  echo "backup: source maps archive -> ${sourcemaps_archive}"
fi

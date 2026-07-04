#!/bin/sh
# Crashlens Postgres bootstrap: create the NON-superuser runtime login role.
#
# WHY THIS EXISTS
# ---------------
# The official postgres image makes POSTGRES_USER a SUPERUSER, and PostgreSQL
# superusers BYPASS Row Level Security entirely. If the application connected as
# that user the structural tenant isolation the schema builds (FORCE RLS + the
# crashlens_app / crashlens_system / crashlens_admin NOLOGIN privilege roles)
# would be silently inert. So we split the database access into two users:
#
#   * POSTGRES_USER  - superuser, OWNS the schema, runs migrations (DDL). The
#                      application never connects as this user.
#   * crashlens_login - NON-superuser LOGIN role the api and worker connect as
#                      at runtime (DATABASE_URL). Because it is not a superuser,
#                      RLS actually constrains it. It carries no privileges of
#                      its own; it inherits them by being a member of the three
#                      NOLOGIN privilege roles.
#
# WHEN THIS RUNS
# --------------
# The postgres image executes every /docker-entrypoint-initdb.d/*.sh script ONCE,
# at first cluster initialisation (empty data directory), connected as the
# POSTGRES_USER superuser. It does NOT run on subsequent restarts. See the
# UPGRADE PATH note below for pre-existing clusters.
#
# TIMING INTERLOCK WITH THE MIGRATIONS
# ------------------------------------
# At first init the Alembic migrations have NOT run yet, so the NOLOGIN privilege
# roles (crashlens_app / crashlens_system / crashlens_admin) do not exist. This
# script therefore creates them idempotently ITSELF, then grants them to
# crashlens_login. When migrations later run, their own idempotent role creation
# (CREATE ROLE ... only IF NOT EXISTS, in DO blocks) no-ops harmlessly and
# applies its table grants to the already-existing roles. Role membership
# inheritance is live, so crashlens_login gains those privileges the moment the
# migrations grant them - no re-grant to crashlens_login is needed.
#
# UPGRADE PATH FOR A PRE-EXISTING CLUSTER (this script did not run)
# ----------------------------------------------------------------
# If your Postgres data volume was created before this script existed, run the
# following ONCE as the superuser (substitute your app password), then point
# DATABASE_URL at crashlens_login:
#
#   psql "$MIGRATIONS_DATABASE_URL" -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN \
#     IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='crashlens_app')    THEN CREATE ROLE crashlens_app NOLOGIN; END IF; \
#     IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='crashlens_system') THEN CREATE ROLE crashlens_system NOLOGIN BYPASSRLS; END IF; \
#     IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='crashlens_admin')  THEN CREATE ROLE crashlens_admin NOLOGIN BYPASSRLS; END IF; \
#     IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='crashlens_login')  THEN CREATE ROLE crashlens_login LOGIN NOSUPERUSER PASSWORD 'YOUR_APP_PASSWORD'; END IF; \
#     GRANT crashlens_app, crashlens_system, crashlens_admin TO crashlens_login; END \$\$;"
#
set -eu

if [ -z "${CRASHLENS_DB_APP_PASSWORD:-}" ]; then
	echo "postgres-init: CRASHLENS_DB_APP_PASSWORD is not set; refusing to create the crashlens_login runtime role without a password." >&2
	exit 1
fi

# psql expands :'app_password' (safely quoted) in the top-level statement below,
# but NOT inside a dollar-quoted DO block, so the password is stashed in a
# session GUC first and read back with current_setting() inside the DO block.
# The password is never written to a log: no --echo flags are set and
# ON_ERROR_STOP aborts before any statement text would be printed.
psql -v ON_ERROR_STOP=1 \
	--username "$POSTGRES_USER" \
	--dbname "$POSTGRES_DB" \
	--set=app_password="$CRASHLENS_DB_APP_PASSWORD" <<-'EOSQL'
	-- Stash the app password in a namespaced session GUC (top level: psql
	-- expands :'app_password' here and quotes it as a safe string literal).
	SELECT set_config('crashlens.app_password', :'app_password', false);

	DO $$
	DECLARE
		app_pw text := current_setting('crashlens.app_password');
	BEGIN
		-- 1. Create the NOLOGIN privilege roles the migrations expect, in case
		--    they have not been created yet (first init runs before migrations).
		--    The migrations re-create these idempotently and grant tables to
		--    them; membership inheritance makes crashlens_login pick those up.
		IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_app') THEN
			CREATE ROLE crashlens_app NOLOGIN;
		END IF;
		IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_system') THEN
			CREATE ROLE crashlens_system NOLOGIN BYPASSRLS;
		END IF;
		IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_admin') THEN
			CREATE ROLE crashlens_admin NOLOGIN BYPASSRLS;
		END IF;

		-- 2. Create (or repair) the runtime login role: NON-superuser so RLS
		--    binds it. format(%L) safely quotes the password from the GUC.
		IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_login') THEN
			EXECUTE format('CREATE ROLE crashlens_login LOGIN NOSUPERUSER INHERIT PASSWORD %L', app_pw);
		ELSE
			EXECUTE format('ALTER ROLE crashlens_login WITH LOGIN NOSUPERUSER INHERIT PASSWORD %L', app_pw);
		END IF;

		-- 3. Make the runtime role a member of the privilege bundles. crashlens_admin
		--    is granted defensively only if present (it always is after step 1,
		--    but this keeps the grant safe against future role reshuffles).
		GRANT crashlens_app TO crashlens_login;
		GRANT crashlens_system TO crashlens_login;
		IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_admin') THEN
			GRANT crashlens_admin TO crashlens_login;
		END IF;
	END
	$$;

	-- Drop the password out of the session GUC so it does not linger.
	SELECT set_config('crashlens.app_password', '', false);
EOSQL

echo "postgres-init: crashlens_login runtime role is ready (non-superuser, RLS-bound)."

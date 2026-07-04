"""Contract test: the shipped self-host default enforces tenant isolation.

This parses the COMMITTED deployment files as data (no live services, no
database), so it runs everywhere and keeps a future edit from silently reverting
the isolation default: the moment someone points the app's DATABASE_URL back at
the superuser, or drops the init role bootstrap, this fails.

The gap it guards against: the postgres image makes POSTGRES_USER a superuser,
and superusers BYPASS Row Level Security. If the app connected as that user the
schema's FORCE-RLS tenant isolation would be silently inert. The fix runs the
app as the non-superuser crashlens_login role instead.
"""

import re
from pathlib import Path

import yaml

# server/tests/ -> server/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO_ROOT / "docker-compose.yml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_INIT_SCRIPT = _REPO_ROOT / "deploy" / "postgres-init" / "01-app-user.sh"

_INIT_MOUNT = "./deploy/postgres-init:/docker-entrypoint-initdb.d:ro"
_RUNTIME_DB_USER = "crashlens_login"
_SUPERUSER = "crashlens"
_NOLOGIN_ROLES = ("crashlens_app", "crashlens_system", "crashlens_admin")


def _env_values() -> dict[str, str]:
    """Parse .env.example KEY=VALUE lines (ignoring comments/blank lines)."""
    values: dict[str, str] = {}
    for raw in _ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()
    return values


def _db_url_user(url: str) -> str:
    """Extract the connection user from a postgresql+asyncpg URL."""
    match = re.match(r"^[^:]+://([^:@/]+)[:@]", url)
    assert match, f"could not parse a user from DATABASE_URL: {url!r}"
    return match.group(1)


def test_runtime_database_url_uses_non_superuser_login_role() -> None:
    """The app's DATABASE_URL must connect as crashlens_login, not the superuser."""
    env = _env_values()
    assert "DATABASE_URL" in env, ".env.example must define DATABASE_URL"
    user = _db_url_user(env["DATABASE_URL"])
    assert user == _RUNTIME_DB_USER, (
        f"DATABASE_URL must connect as the non-superuser {_RUNTIME_DB_USER!r} so "
        f"RLS tenant isolation is enforced; found {user!r}"
    )
    assert user != env.get("POSTGRES_USER"), (
        "DATABASE_URL must NOT reuse POSTGRES_USER (the RLS-bypassing superuser)"
    )


def test_migrations_url_uses_the_schema_owning_superuser() -> None:
    """Migrations run as the superuser (DDL needs ownership) via a separate URL."""
    env = _env_values()
    assert "MIGRATIONS_DATABASE_URL" in env, (
        ".env.example must define MIGRATIONS_DATABASE_URL for the migrate step"
    )
    migrate_user = _db_url_user(env["MIGRATIONS_DATABASE_URL"])
    assert migrate_user == env.get("POSTGRES_USER") == _SUPERUSER, (
        f"MIGRATIONS_DATABASE_URL must connect as the superuser POSTGRES_USER "
        f"({_SUPERUSER!r}); found {migrate_user!r}"
    )
    assert "CRASHLENS_DB_APP_PASSWORD" in env, (
        ".env.example must define CRASHLENS_DB_APP_PASSWORD for the runtime role"
    )


def test_env_example_holds_only_placeholders_no_real_secrets() -> None:
    """Every credential in .env.example must be an obvious placeholder."""
    env = _env_values()
    for key in (
        "POSTGRES_PASSWORD",
        "CRASHLENS_DB_APP_PASSWORD",
        "DATABASE_URL",
        "MIGRATIONS_DATABASE_URL",
    ):
        assert "REPLACE_WITH" in env[key], (
            f"{key} in .env.example must be a REPLACE_WITH placeholder, not a real value"
        )


def _postgres_service() -> dict:
    compose = yaml.safe_load(_COMPOSE.read_text())
    services = compose["services"]
    assert "postgres" in services, "compose must define a postgres service"
    return services["postgres"]


def test_compose_mounts_the_init_dir_and_passes_the_app_password() -> None:
    """postgres must mount the init dir and receive CRASHLENS_DB_APP_PASSWORD."""
    postgres = _postgres_service()
    volumes = postgres.get("volumes", [])
    assert _INIT_MOUNT in volumes, (
        f"postgres service must mount the init script dir ({_INIT_MOUNT}); "
        f"found volumes: {volumes}"
    )
    env = postgres.get("environment", {})
    # environment may be a dict or a list of KEY: VALUE / KEY=VALUE strings.
    if isinstance(env, dict):
        env_keys = set(env)
    else:
        env_keys = {re.split(r"[:=]", e, maxsplit=1)[0].strip() for e in env}
    assert "CRASHLENS_DB_APP_PASSWORD" in env_keys, (
        "postgres service must pass CRASHLENS_DB_APP_PASSWORD to the init script"
    )


def test_init_script_creates_all_roles_and_the_login_role() -> None:
    """The init script must create the three NOLOGIN roles + crashlens_login."""
    script = _INIT_SCRIPT.read_text()
    assert _INIT_SCRIPT.exists()
    for role in _NOLOGIN_ROLES:
        assert f"CREATE ROLE {role} NOLOGIN" in script, (
            f"init script must idempotently create {role} (before migrations run)"
        )
    assert "CREATE ROLE crashlens_login LOGIN NOSUPERUSER" in script, (
        "init script must create crashlens_login as a NON-superuser login role"
    )
    for role in _NOLOGIN_ROLES:
        assert f"GRANT {role} TO crashlens_login" in script, (
            f"init script must grant {role} to crashlens_login"
        )


def test_init_script_has_no_plaintext_password() -> None:
    """The password must only ever come from the env var, never a literal."""
    script = _INIT_SCRIPT.read_text()
    assert "CRASHLENS_DB_APP_PASSWORD" in script, (
        "init script must source the password from the env var"
    )
    # The role password is applied via format(... %L, <var>), never a literal.
    assert "PASSWORD %L" in script, (
        "init script must set the password via format(%L, <var>), not a literal"
    )
    # No REPLACE_WITH placeholder anywhere.
    assert "REPLACE_WITH" not in script
    # No quoted literal password in EXECUTABLE lines (the header comment carries a
    # documented YOUR_APP_PASSWORD placeholder for the manual upgrade path, which
    # is not a secret; only non-comment lines are checked).
    executable = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r"PASSWORD\s+'[^']", executable), (
        "init script must not contain a quoted literal password in executable SQL"
    )

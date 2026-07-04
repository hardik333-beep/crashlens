# Launch readiness

This is an honest, per-item readiness report for Crashlens as a self-hosted,
open source product. It splits every production-hardening concern into three
buckets:

- **Proven in CI**: there is an automated, hard-failing check on every push that
  exercises the real behaviour. If it regresses, the build goes red.
- **Self-hoster's responsibility**: things Crashlens cannot prove for you
  because they depend on your infrastructure, your domain, and your operational
  discipline. The docs tell you exactly what to do; running it is on you.
- **Deferred**: known gaps we have chosen not to close yet, each with a reason
  and no security impact on the isolation model.

The CI workflow lives in `.github/workflows/ci.yml`. Its jobs are: `lint`,
`test`, `migrations`, `gitleaks`, `cross-tenant-isolation`, `dashboard-build`,
and `caddy-validate`.

## Proven in CI

| Concern | Evidence | Where |
| --- | --- | --- |
| Tenant isolation is structural, not hand-written | The app under test connects as the non-superuser `crashlens_login` role, so PostgreSQL Row Level Security is genuinely in force on the HTTP path (not bypassed by a superuser connection). A hard-failing gate runs every `isolation`-marked test: Tenant A cannot read or act on Tenant B's data, and a cross-org project id returns 404. | `cross-tenant-isolation` job; `test_cross_tenant_gate.py` plus the `isolation`-marked tests across the suite; role setup in the `test` and `cross-tenant-isolation` jobs |
| Row Level Security actually binds the app | The `test` and `cross-tenant-isolation` jobs create `crashlens_login` (NON-superuser) after migrations and point `DATABASE_URL` at it, mirroring `deploy/postgres-init/01-app-user.sh`. Setup fixtures keep a separate superuser URL via `SUPERUSER_DATABASE_URL`. | `test` and `cross-tenant-isolation` jobs; `tests/conftest.py` `superuser_database_url()` |
| Account-takeover resistance | Per-account lockout after repeated failures with an unlock window, and identical 401 responses for unknown, wrong, and locked accounts (no user enumeration, uniform failure). | `test_login_locks_after_ten_failures_and_unlocks_after_window`, `test_login_returns_identical_401_for_unknown_wrong_and_locked`, `test_authenticate_returns_none_uniformly_for_all_failure_modes` (`test_auth_integration.py`) |
| Ingest abuse and spend containment | The ingest hot path enforces a rate limit (429 with `Retry-After`, and the oversized body is never processed), plus per-project sampling that can drop events before enqueue. The edge proxy additionally caps request bodies (250KB ingest, 25MB source maps). | `test_rate_limit_429_with_retry_after_and_body_never_processed` (`test_ingest_integration.py`); `deploy/Caddyfile` |
| Migrations are reversible | Every migration is upgraded to head then downgraded to base against a throwaway Postgres. A one-way migration fails the build. | `migrations` job |
| No secrets in the tree or history | Full-history secret scan on every push. | `gitleaks` job |
| Query performance at scale | A 50,000-event seed across 14 daily partitions proves the issues-list query and the 14-day occurrences query stay well under a 2s ceiling, and that EXPLAIN shows no sequential scan on any events partition (the `(project_id, issue_id, received_at)` index is used and partitions are pruned). | `test_perf_smoke.py` (runs in the `test` job) |
| Reverse-proxy config is valid | The Caddyfile is validated with the official Caddy image before it can ship, so a proxy typo fails CI instead of the first deploy. | `caddy-validate` job |
| Dashboard builds and typechecks | Production build plus eslint and `tsc --noEmit`. | `dashboard-build` and `lint` jobs |

## Self-hoster's responsibility

These depend on your environment. Crashlens ships the mechanism and the docs;
you run and verify them.

| Concern | What you must do | Reference |
| --- | --- | --- |
| Email deliverability | Email alerts are optional and off until you configure SMTP (`SMTP_HOST`, `SMTP_FROM`, and friends). If you enable them, use a reputable relay and set up SPF, DKIM, and DMARC for your sending domain so alert mail is not dropped as spam. Crashlens cannot verify your DNS. | `docs/configuration.md`; `app/config.py` SMTP settings |
| Backups and a tested restore | Postgres holds all issue and event data. Take regular backups and, more importantly, actually rehearse a restore. A backup you have never restored is a guess, not a safety net. | `docs/backup-restore.md` |
| HTTPS on a real domain | Set `CRASHLENS_SITE_ADDRESS` to your public hostname so Caddy provisions automatic HTTPS. The default is plain HTTP on `:80` for local development only; do not expose that to the internet. | `docs/self-hosting.md`; `deploy/Caddyfile` |
| Spend caps | Not applicable. Crashlens is self-hosted and calls no metered third-party APIs, so there is no runaway cloud bill to cap. Your only cost ceiling is your own server, which retention (partition drop plus per-project trim) keeps bounded. | `server/app/jobs/retention.py` |

## Deferred (known, with rationale)

| Item | Why deferred | Risk |
| --- | --- | --- |
| End-to-end Playwright click suite and SDK end-to-end tests | These need a full Docker Compose stack (proxy, API, worker, dashboard, Postgres, Redis) running locally, which is pending on the maintainer machine. The full backend and isolation behaviour is already proven by the API-level integration suite against a real Postgres in CI. | Low. Backend correctness and isolation are covered; what is not yet automated is browser-level and cross-SDK wiring. |
| Observability (Sentry-style dogfooding of Crashlens on Crashlens) | We have not yet pointed a Crashlens instance at itself for its own error capture, nor wired external alerting for the CI or a running instance. Structured logging exists in the app; a full observability stack is future work. | Low to medium for an operator. Self-hosters should watch container logs until this lands. |
| Ingest 401 negative-cache optimization | An invalid DSN currently hits the database lookup on every request rather than being negatively cached, so a flood of bad keys costs one cheap indexed lookup each. It is correct and rate-limited, just not yet optimized. | Low. Bounded by the ingest rate limit and a single indexed query. |

## One-line verdict

The isolation model, auth hardening, rate limiting, migration reversibility,
secret hygiene, and query performance are all proven by hard-failing CI on every
push. Deliverability, backups, and HTTPS are yours to run. The deferred items are
convenience and dogfooding gaps, not holes in the tenant isolation guarantee.

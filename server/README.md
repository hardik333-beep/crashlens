# Crashlens server

FastAPI application and arq worker for Crashlens.

## Local development

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Provide config (or export the variables directly).
export DATABASE_URL=postgresql+asyncpg://crashlens:crashlens@localhost:5432/crashlens
export REDIS_URL=redis://localhost:6379/0
export SECRET_KEY=dev-only-not-a-real-secret

uvicorn app.main:app --reload   # API
arq app.worker.WorkerSettings   # worker
pytest                          # tests (do not require a live database)
```

## Migrations

Alembic is initialised with no revisions yet. Every future revision must ship an
explicit reversible `upgrade` and `downgrade`.

```bash
alembic upgrade head
alembic downgrade base
```

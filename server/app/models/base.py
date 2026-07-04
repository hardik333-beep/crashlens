"""Declarative base for the Crashlens ORM models.

These models mirror the hand-authored Alembic schema in
``alembic/versions/0001_v1_schema.py``. They are NOT wired to Alembic
autogenerate (``target_metadata`` stays ``None`` in ``alembic/env.py``): the
migration is the source of truth, and these classes exist so the application can
build typed, RLS-scoped queries. Keep the two in sync by hand.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all Crashlens ORM models."""

"""Test configuration.

Provides throwaway values for the required settings so the application can be
constructed without a live environment. These are NOT real credentials; the
health probes will simply report the datastores as unreachable, which is the
behaviour the smoke test asserts is safe.
"""

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://crashlens:crashlens@localhost:5432/crashlens",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-not-a-real-key")
os.environ.setdefault("ENVIRONMENT", "test")

"""Loop-coherence contract for the async suite (no database required).

Proves directly, without Postgres, that under this project's pytest-asyncio
configuration a higher-scoped (session) async fixture and the async tests that
consume it run on the SAME event loop object.

Why this matters: the db-integration suites bind module-scoped
engines/connections inside async fixtures and then use them from many tests.
With pytest-asyncio's default function-scoped test loop, those asyncpg
connections are created on one loop and used/disposed across others, which
raises "cannot perform operation: another operation is in progress" at setup
and "RuntimeError: Event loop is closed" at teardown -- exactly the failure
that broke the ``pytest`` and ``cross-tenant isolation`` CI jobs.

Under the broken default this test FAILS (the fixture captures the session loop
while each test runs on its own function loop, so the ``is`` identity check does
not hold). Under the session-scoped loop configured in ``pyproject.toml``
(``asyncio_default_fixture_loop_scope`` and ``asyncio_default_test_loop_scope``
both ``"session"``) every fixture and test share one loop and it PASSES. It is a
permanent guard against silently reverting that configuration.
"""

import asyncio

import pytest_asyncio


@pytest_asyncio.fixture(scope="session")
async def fixture_loop() -> asyncio.AbstractEventLoop:
    """Capture the running loop the session-scoped async fixture executes on."""
    return asyncio.get_running_loop()


async def test_fixture_and_test_share_one_loop(fixture_loop) -> None:
    """A test using the session fixture runs on the fixture's loop."""
    assert asyncio.get_running_loop() is fixture_loop


async def test_second_test_shares_the_same_loop(fixture_loop) -> None:
    """A second test also runs on that same one loop (the module-scoped case)."""
    assert asyncio.get_running_loop() is fixture_loop

"""50k-event seed performance smoke (db-marked; skips locally, runs in CI).

Seeds one org / one project / many issues and 50,000 events spread across 14
daily partitions, then proves the two hot read paths stay healthy at that scale:

* the issues-list query and the 14-day per-issue occurrences query (the exact
  queries in ``app/issues.py``: :func:`app.issues.list_issues` and the grouped
  occurrences query inside :func:`app.issues.get_issue`) each complete well
  under a generous wall-clock ceiling;
* the occurrences query's plan is partition-pruned and index-driven: no
  ``Seq Scan`` on any ``events`` partition. The composite index
  ``ix_events_project_issue_received (project_id, issue_id, received_at)`` from
  migration 0001 covers exactly that predicate; this test is the guard that it
  stays that way. If a schema change ever regressed the plan to a sequential
  scan, the fix is a new reversible migration adding the missing index, never a
  weaker assertion here.

Seeding uses batched ``executemany`` inserts under the superuser engine (RLS
bypassed for setup), which loads 50k rows in a few seconds. The measured read
queries run through the normal service functions on the default session factory,
so in CI they execute as the non-superuser ``crashlens_login`` role with RLS in
force, exactly like production.
"""

import datetime
import json
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import issues
from app.db import tenant_session
from tests.conftest import ensure_events_partitions, superuser_database_url

pytestmark = pytest.mark.db

# 50,000 events is the seed target; spreading them over 14 daily partitions and
# many issues keeps any single issue selective, so the (project_id, issue_id,
# received_at) index is clearly the best plan for the per-issue occurrences query.
_TOTAL_EVENTS = 50_000
_WINDOW_DAYS = 14
_ISSUE_COUNT = 200
_INSERT_BATCH = 5_000

# Generous ceilings: on the seeded set with the index in place both queries run
# in low tens of milliseconds; 2s leaves ample headroom for a loaded CI runner
# while still catching an accidental full-table-scan regression (which would be
# seconds to minutes at this row count).
_CEILING_SECONDS = 2.0


@pytest_asyncio.fixture(scope="module")
async def superuser_engine():
    """Engine on the migration/superuser URL. Skips if Postgres is unreachable."""
    engine = create_async_engine(superuser_database_url())
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable; skipping perf smoke test")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="module")
async def seeded(superuser_engine):
    """Seed one org/project, ``_ISSUE_COUNT`` issues, and 50k events over 14 days.

    Returns the ids plus the window start the occurrences query uses, so the
    test can measure and EXPLAIN the exact production query. Rows are removed on
    teardown (events carry no FK, so they are deleted explicitly before the org
    cascade clears projects/issues).
    """
    today = datetime.datetime.now(datetime.UTC).date()
    days = [today - datetime.timedelta(days=n) for n in range(_WINDOW_DAYS)]
    await ensure_events_partitions(superuser_engine, days)

    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    issue_ids = [uuid.uuid4() for _ in range(_ISSUE_COUNT)]
    target_issue_id = issue_ids[0]

    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": org_id, "name": "perf", "slug": f"perf-{org_id}"},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, org_id, name, slug, platform) "
                "VALUES (:id, :org, 'perf', :slug, 'python')"
            ),
            {"id": project_id, "org": org_id, "slug": f"perf-proj-{project_id}"},
        )
        await conn.execute(
            text(
                "INSERT INTO issues (id, org_id, project_id, fingerprint, title, level, "
                "event_count) VALUES "
                "(:id, :org, :proj, :fp, :title, 'error', :count)"
            ),
            [
                {
                    "id": issue_id,
                    "org": org_id,
                    "proj": project_id,
                    "fp": str(issue_id),
                    "title": f"Perf issue {i}",
                    "count": _TOTAL_EVENTS // _ISSUE_COUNT,
                }
                for i, issue_id in enumerate(issue_ids)
            ],
        )

        # 50k events: the issue cycles every event and the day cycles every full
        # pass over the issues, so the two indices never alias -- every issue is
        # spread evenly across all 14 days, and every partition holds a slice of
        # every issue. received_at at noon UTC lands squarely inside that day's
        # partition range.
        insert_sql = text(
            "INSERT INTO events "
            "(org_id, project_id, issue_id, event_id, received_at, environment, level, payload) "
            "VALUES (:org, :proj, :issue, :eid, :ts, 'production', 'error', '{}'::jsonb)"
        )
        batch: list[dict] = []
        for n in range(_TOTAL_EVENTS):
            day = days[(n // _ISSUE_COUNT) % _WINDOW_DAYS]
            ts = datetime.datetime.combine(
                day, datetime.time(12, 0), tzinfo=datetime.UTC
            )
            batch.append(
                {
                    "org": org_id,
                    "proj": project_id,
                    "issue": issue_ids[n % _ISSUE_COUNT],
                    "eid": uuid.uuid4(),
                    "ts": ts,
                }
            )
            if len(batch) >= _INSERT_BATCH:
                await conn.execute(insert_sql, batch)
                batch = []
        if batch:
            await conn.execute(insert_sql, batch)

    window_start = datetime.datetime.combine(
        today - datetime.timedelta(days=_WINDOW_DAYS - 1),
        datetime.time.min,
        tzinfo=datetime.UTC,
    )
    yield {
        "org_id": org_id,
        "project_id": project_id,
        "target_issue_id": target_issue_id,
        "window_start": window_start,
    }

    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM events WHERE org_id = :org"), {"org": org_id}
        )
        await conn.execute(text("DELETE FROM orgs WHERE id = :org"), {"org": org_id})


async def test_issues_list_query_under_ceiling(seeded) -> None:
    """The issues-list query stays well under the ceiling at 50k events."""
    start = time.perf_counter()
    result = await issues.list_issues(seeded["org_id"], seeded["project_id"])
    elapsed = time.perf_counter() - start
    assert result is not None
    assert result["total"] == _ISSUE_COUNT
    assert elapsed < _CEILING_SECONDS, (
        f"issues list query took {elapsed:.3f}s, ceiling {_CEILING_SECONDS}s"
    )


async def test_occurrences_query_under_ceiling(seeded) -> None:
    """The 14-day occurrences query (inside get_issue) stays under the ceiling."""
    start = time.perf_counter()
    detail = await issues.get_issue(
        seeded["org_id"], seeded["project_id"], seeded["target_issue_id"]
    )
    elapsed = time.perf_counter() - start
    assert detail is not None
    # The window is always exactly 14 zero-filled buckets; the even seed spread
    # means every one of them is non-zero for the target issue.
    assert len(detail["occurrences"]) == _WINDOW_DAYS
    assert all(bucket["count"] > 0 for bucket in detail["occurrences"])
    total = sum(bucket["count"] for bucket in detail["occurrences"])
    assert total == _TOTAL_EVENTS // _ISSUE_COUNT
    assert elapsed < _CEILING_SECONDS, (
        f"occurrences query took {elapsed:.3f}s, ceiling {_CEILING_SECONDS}s"
    )


def _seq_scanned_events_partitions(plan_node: dict) -> list[str]:
    """Return the relation names of any ``Seq Scan`` node over an ``events`` table."""
    found: list[str] = []
    node_type = plan_node.get("Node Type", "")
    relation = plan_node.get("Relation Name", "")
    if node_type == "Seq Scan" and relation.startswith("events"):
        found.append(relation)
    for child in plan_node.get("Plans", []):
        found.extend(_seq_scanned_events_partitions(child))
    return found


async def test_occurrences_query_plan_is_index_and_partition_pruned(seeded) -> None:
    """EXPLAIN the occurrences query: no Seq Scan on any events partition.

    The range bound is inlined as a literal (it is a server-computed date, never
    user input) so partition pruning happens at plan time and shows in EXPLAIN
    without ANALYZE.
    """
    start_literal = seeded["window_start"].isoformat()
    explain_sql = text(
        "EXPLAIN (FORMAT JSON) "
        "SELECT (received_at AT TIME ZONE 'UTC')::date AS day, count(*) AS c "
        "FROM events WHERE project_id = :pid AND issue_id = :iid "
        f"AND received_at >= '{start_literal}'::timestamptz "
        "GROUP BY (received_at AT TIME ZONE 'UTC')::date"
    )
    async with tenant_session(str(seeded["org_id"])) as session:
        raw = (
            await session.execute(
                explain_sql,
                {
                    "pid": str(seeded["project_id"]),
                    "iid": str(seeded["target_issue_id"]),
                },
            )
        ).scalar_one()

    plan = json.loads(raw) if isinstance(raw, str) else raw
    root = plan[0]["Plan"]
    seq_scanned = _seq_scanned_events_partitions(root)
    assert seq_scanned == [], (
        "occurrences query fell back to a sequential scan on events partition(s) "
        f"{seq_scanned}; the (project_id, issue_id, received_at) index is not "
        "being used -- add the missing index in a new migration rather than "
        "weakening this assertion"
    )

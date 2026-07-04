"""Metadata guards for alembic revisions. No database required.

Alembic's ``alembic_version`` table stores the current revision id in a
VARCHAR(32) column. A revision id longer than 32 characters passes every
offline check (``alembic upgrade head --sql`` renders fine, since the version
table only exists on a live database) and then fails the FIRST live migration
run with asyncpg's StringDataRightTruncationError: "value too long for type
character varying(32)". That exact failure shipped once (revision 0003's
original 34-char id); these tests make it structurally impossible to recur,
and also assert the down_revision chain is intact (every revision points at a
revision that actually exists, exactly one root, no duplicates).
"""

import importlib.util
import re
from pathlib import Path

# Alembic's version table column: sqlalchemy.String(32) in
# alembic.runtime.migration (the schema alembic creates by default).
_ALEMBIC_VERSION_MAX_LEN = 32

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _load_revision_metadata() -> dict[str, str | None]:
    """Import every revision module and return {revision: down_revision}.

    Real imports (not regex over source) so the values checked are exactly
    what alembic itself will see at runtime.
    """
    metadata: dict[str, str | None] = {}
    version_files = sorted(_VERSIONS_DIR.glob("*.py"))
    assert version_files, f"no revision files found in {_VERSIONS_DIR}"
    for path in version_files:
        spec = importlib.util.spec_from_file_location(f"_alembic_meta_{path.stem}", path)
        assert spec is not None and spec.loader is not None, f"cannot load {path.name}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        revision = getattr(module, "revision", None)
        assert isinstance(revision, str) and revision, (
            f"{path.name}: missing or empty `revision` string"
        )
        assert hasattr(module, "down_revision"), f"{path.name}: missing `down_revision`"
        assert revision not in metadata, f"duplicate revision id {revision!r} ({path.name})"
        metadata[revision] = module.down_revision
    return metadata


def test_every_revision_id_fits_alembic_version_varchar32() -> None:
    for revision, down_revision in _load_revision_metadata().items():
        assert len(revision) <= _ALEMBIC_VERSION_MAX_LEN, (
            f"revision id {revision!r} is {len(revision)} chars; alembic_version "
            f"is VARCHAR({_ALEMBIC_VERSION_MAX_LEN}) and a longer id fails the "
            f"first LIVE migration run (offline --sql emission does not catch it)"
        )
        if down_revision is not None:
            assert len(down_revision) <= _ALEMBIC_VERSION_MAX_LEN, (
                f"down_revision {down_revision!r} is {len(down_revision)} chars, "
                f"over the VARCHAR({_ALEMBIC_VERSION_MAX_LEN}) limit"
            )


def test_down_revision_chain_is_intact() -> None:
    metadata = _load_revision_metadata()

    roots = [rev for rev, down in metadata.items() if down is None]
    assert len(roots) == 1, f"expected exactly one root revision, found {roots!r}"

    for revision, down_revision in metadata.items():
        if down_revision is None:
            continue
        assert down_revision in metadata, (
            f"revision {revision!r} points at down_revision {down_revision!r}, "
            f"which does not exist in {_VERSIONS_DIR}"
        )

    # No two revisions share a parent (a linear history, no unintended
    # branches; alembic would otherwise require explicit branch labels).
    parents = [down for down in metadata.values() if down is not None]
    assert len(parents) == len(set(parents)), (
        "two revisions share the same down_revision: unintended branch in the "
        "migration history"
    )


def test_revision_filename_matches_revision_id() -> None:
    # Convention guard: file 0003_partition_fn_secdef.py must declare
    # revision "0003_partition_fn_secdef" so ids stay greppable on disk.
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        content = path.read_text(encoding="utf-8")
        match = re.search(r'^revision(?::\s*str)?\s*=\s*"([^"]+)"', content, re.MULTILINE)
        assert match is not None, f"{path.name}: no revision assignment found"
        assert match.group(1) == path.stem, (
            f"{path.name}: revision id {match.group(1)!r} does not match filename "
            f"stem {path.stem!r}"
        )

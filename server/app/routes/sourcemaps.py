"""Source map upload / list / delete endpoints (admin only).

Mounted without an /api prefix (the reverse proxy strips /api), so
POST /api/orgs/{org_id}/projects/{project_id}/sourcemaps from the browser reaches
/orgs/{org_id}/projects/{project_id}/sourcemaps here.

Every handler depends on ``require_org_admin``, so the org id in the path is
verified against the caller's admin membership BEFORE any filesystem path is
built. The project id is additionally confirmed to belong to that org (via
``projects.get_project`` under RLS) so a valid admin of org A cannot seed files
under a project that is not theirs. Files land at::

    {SOURCEMAPS_DIR}/{org_id}/{project_id}/{release_dir}/{basename}

where ``release_dir`` is the reversible base64url encoding of the release string
(see ``app/sourcemaps.py``) and ``basename`` is reduced by ``safe_basename`` --
the client's field name and any directory component it sends are discarded.
Client paths are NEVER trusted.

CAPS (FLAGGED DEFAULTS; governor review): at most ``MAX_FILES`` files per request
and ``MAX_FILE_BYTES`` per file. The per-file byte cap is enforced incrementally
while streaming each upload to disk, so an oversized file is rejected with 413
without ever being fully buffered by this handler. The edge (Caddy) additionally
caps the whole request body at 25MB on this route (deploy/Caddyfile).

SECRETS / PII HYGIENE: logs only ids, filenames, counts, and byte sizes, never
map contents.
"""

import logging
import os
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app import projects, sourcemaps
from app.auth import OrgContext, require_org_admin
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sourcemaps"])

# FLAGGED DEFAULTS (governor review).
MAX_FILES = 20
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB per file.
_CHUNK_BYTES = 1024 * 1024  # stream in 1 MB chunks.


class SourcemapFileOut(BaseModel):
    basename: str
    size: int
    uploaded_at: str


class SourcemapReleaseOut(BaseModel):
    release: str
    file_count: int
    files: list[SourcemapFileOut]


_PROJECT_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Project not found.",
)


async def _verify_project(ctx: OrgContext, project_id: uuid.UUID) -> None:
    """Confirm ``project_id`` belongs to the verified org, or raise 404.

    Uses the RLS-scoped ``projects.get_project``: a project in another org is not
    visible and yields a 404, never a cross-tenant path write.
    """
    project = await projects.get_project(ctx.org_id, project_id)
    if project is None:
        raise _PROJECT_NOT_FOUND


def _release_out(entry: dict) -> SourcemapReleaseOut:
    files = [SourcemapFileOut(**f) for f in entry["files"]]
    return SourcemapReleaseOut(
        release=entry["release"], file_count=len(files), files=files
    )


@router.get(
    "/orgs/{org_id}/projects/{project_id}/sourcemaps",
    response_model=list[SourcemapReleaseOut],
)
async def list_sourcemaps(
    project_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> list[SourcemapReleaseOut]:
    await _verify_project(ctx, project_id)
    settings = get_settings()
    entries = sourcemaps.list_release_maps(
        settings.sourcemaps_dir, str(ctx.org_id), str(project_id)
    )
    return [_release_out(entry) for entry in entries]


async def _stream_to_disk(upload: UploadFile, dest_path: str) -> int:
    """Stream ``upload`` to ``dest_path`` (atomic via a temp file), enforcing the cap.

    Writes to ``{dest_path}.tmp`` while counting bytes; on exceeding
    ``MAX_FILE_BYTES`` it deletes the partial file and raises 413. On success it
    atomically renames the temp file into place. Returns the byte size written.
    """
    tmp_path = dest_path + ".tmp"
    total = 0
    try:
        with open(tmp_path, "wb") as handle:
            while True:
                chunk = await upload.read(_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            "A source map file exceeds the maximum allowed size "
                            f"of {MAX_FILE_BYTES // (1024 * 1024)} MB."
                        ),
                    )
                handle.write(chunk)
        os.replace(tmp_path, dest_path)
    except BaseException:
        # On any failure (cap exceeded, disk error, client disconnect) leave no
        # partial file behind.
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return total


@router.post(
    "/orgs/{org_id}/projects/{project_id}/sourcemaps",
    status_code=status.HTTP_201_CREATED,
    response_model=SourcemapReleaseOut,
)
async def upload_sourcemaps(
    project_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
    release: Annotated[str, Form()],
    files: list[UploadFile],
) -> SourcemapReleaseOut:
    """Store one or more ``.map`` files for a release (admin only).

    ``release`` is a required non-empty form field. Each uploaded file's FILENAME
    is the map basename; non-``.map`` files are rejected with 400. The client's
    field name and any directory part of the filename are ignored.
    """
    release = release.strip()
    if not release:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A release is required.",
        )
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one source map file is required.",
        )
    if len(files) > MAX_FILES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_FILES} files may be uploaded per request.",
        )

    await _verify_project(ctx, project_id)

    settings = get_settings()
    release_dir = sourcemaps.release_maps_dir(
        settings.sourcemaps_dir, str(ctx.org_id), str(project_id), release
    )

    # Validate every filename BEFORE writing anything, so a single bad file in the
    # batch does not leave a partial upload on disk.
    planned: list[tuple[UploadFile, str]] = []
    for upload in files:
        basename = sourcemaps.safe_basename(upload.filename or "")
        if basename is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A file has an unsafe or empty name.",
            )
        if not basename.endswith(".map"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Only .map files are accepted; '{basename}' is not a .map file."
                ),
            )
        planned.append((upload, basename))

    os.makedirs(release_dir, exist_ok=True)
    for upload, basename in planned:
        dest_path = os.path.join(release_dir, basename)
        size = await _stream_to_disk(upload, dest_path)
        logger.info(
            "sourcemaps: stored org=%s project=%s basename=%s bytes=%d",
            ctx.org_id,
            project_id,
            basename,
            size,
        )

    entries = sourcemaps.list_release_maps(
        settings.sourcemaps_dir, str(ctx.org_id), str(project_id)
    )
    for entry in entries:
        if entry["release"] == release:
            return _release_out(entry)
    # Should be unreachable (we just wrote files for this release), but stay
    # defensive rather than raise a 500.
    return SourcemapReleaseOut(release=release, file_count=0, files=[])


@router.delete(
    "/orgs/{org_id}/projects/{project_id}/sourcemaps/{release}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_sourcemaps(
    project_id: uuid.UUID,
    release: str,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> None:
    """Delete a release's entire source map directory (admin only)."""
    await _verify_project(ctx, project_id)
    settings = get_settings()
    removed = sourcemaps.delete_release_maps(
        settings.sourcemaps_dir, str(ctx.org_id), str(project_id), release
    )
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No source maps found for that release.",
        )

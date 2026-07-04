"""Issue endpoints: list, detail, status actions, and delete.

Mounted without an /api prefix (the reverse proxy strips /api), so
GET /api/orgs/{org_id}/projects/{project_id}/issues from the browser reaches
/orgs/{org_id}/projects/{project_id}/issues here.

Every handler depends on ``require_org_member`` (reads + status actions) or
``require_org_admin`` (delete), so the org id in the path is verified against the
caller's membership BEFORE any read or write. The verified ``ctx.org_id`` is the
only org id passed to the service layer, which scopes all DML through
``tenant_session`` (RLS). No handler trusts a path org id on its own, and none
writes ``WHERE org_id = ...`` by hand. A project, issue, or event in another org
is invisible under RLS and surfaces as a 404.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app import issues
from app.auth import OrgContext, require_org_admin, require_org_member

router = APIRouter(tags=["issues"])


# --- Response models ----------------------------------------------------------
class IssueListItem(BaseModel):
    id: str
    title: str
    level: str
    status: str
    first_seen: str
    last_seen: str
    event_count: int
    assigned_to: str | None


class IssueListOut(BaseModel):
    issues: list[IssueListItem]
    total: int
    page: int
    per_page: int


class OccurrenceDay(BaseModel):
    day: str
    count: int


class RecentEvent(BaseModel):
    event_id: str
    received_at: str
    environment: str
    release: str | None
    level: str


class LatestEvent(BaseModel):
    event_id: str
    received_at: str
    environment: str
    release: str | None
    level: str
    payload: dict[str, Any]


class IssueDetailOut(IssueListItem):
    latest_event: LatestEvent | None
    recent_events: list[RecentEvent]
    occurrences: list[OccurrenceDay]


_ISSUE_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Error not found.",
)


# --- List ---------------------------------------------------------------------
@router.get(
    "/orgs/{org_id}/projects/{project_id}/issues",
    response_model=IssueListOut,
)
async def list_issues(
    project_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_member)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1)] = issues.DEFAULT_PER_PAGE,
) -> IssueListOut:
    try:
        status_value = issues.normalize_status_filter(status_filter)
        sort_value = issues.normalize_sort(sort)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    result = await issues.list_issues(
        ctx.org_id,
        project_id,
        status_filter=status_value,
        q=q.strip() if q else None,
        sort=sort_value,
        page=issues.clamp_page(page),
        per_page=issues.clamp_per_page(per_page),
    )
    if result is None:
        raise _ISSUE_NOT_FOUND
    return IssueListOut.model_validate(result)


# --- Detail -------------------------------------------------------------------
@router.get(
    "/orgs/{org_id}/projects/{project_id}/issues/{issue_id}",
    response_model=IssueDetailOut,
)
async def get_issue(
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_member)],
) -> IssueDetailOut:
    detail = await issues.get_issue(ctx.org_id, project_id, issue_id)
    if detail is None:
        raise _ISSUE_NOT_FOUND
    return IssueDetailOut.model_validate(detail)


# --- Status actions -----------------------------------------------------------
async def _apply_action(
    ctx: OrgContext,
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    action: str,
) -> IssueDetailOut:
    updated = await issues.set_issue_status(ctx.org_id, project_id, issue_id, action)
    if updated is None:
        raise _ISSUE_NOT_FOUND
    # Return the full refreshed detail so the UI updates the header, actions, and
    # (unchanged) occurrence chart from one response.
    detail = await issues.get_issue(ctx.org_id, project_id, issue_id)
    if detail is None:
        raise _ISSUE_NOT_FOUND
    return IssueDetailOut.model_validate(detail)


@router.post(
    "/orgs/{org_id}/projects/{project_id}/issues/{issue_id}/resolve",
    response_model=IssueDetailOut,
)
async def resolve_issue(
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_member)],
) -> IssueDetailOut:
    return await _apply_action(ctx, project_id, issue_id, "resolve")


@router.post(
    "/orgs/{org_id}/projects/{project_id}/issues/{issue_id}/ignore",
    response_model=IssueDetailOut,
)
async def ignore_issue(
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_member)],
) -> IssueDetailOut:
    return await _apply_action(ctx, project_id, issue_id, "ignore")


@router.post(
    "/orgs/{org_id}/projects/{project_id}/issues/{issue_id}/reopen",
    response_model=IssueDetailOut,
)
async def reopen_issue(
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_member)],
) -> IssueDetailOut:
    return await _apply_action(ctx, project_id, issue_id, "reopen")


# --- Delete (admin) -----------------------------------------------------------
@router.delete(
    "/orgs/{org_id}/projects/{project_id}/issues/{issue_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_issue(
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> None:
    deleted = await issues.delete_issue(ctx.org_id, project_id, issue_id)
    if not deleted:
        raise _ISSUE_NOT_FOUND

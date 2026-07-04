"""Audit log read endpoint: the org settings "Activity" view.

Mounted without an /api prefix (the reverse proxy strips /api), so
GET /api/orgs/{org_id}/audit-log from the browser reaches
/orgs/{org_id}/audit-log here.

ADMIN ONLY: this surfaces every sensitive action taken in the org (including
who took it), so it goes through ``require_org_admin``, not
``require_org_member``. The org id in the path is verified against the
caller's admin membership BEFORE any read, and only the verified ``ctx.org_id``
reaches the service layer, which scopes the read through ``tenant_session``
(RLS). A member (non-admin) gets a uniform 403, same as every other admin-only
endpoint in this codebase.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app import accounts, audit
from app.auth import OrgContext, require_org_admin

router = APIRouter(tags=["audit"])


class AuditLogEntryOut(BaseModel):
    id: str
    actor_user_id: str | None
    # Resolved from the RLS-exempt ``users`` table via
    # ``accounts.load_users_by_ids``; null when the actor is unknown (no actor,
    # e.g. a future system action) OR the actor's user row is gone (the FK sets
    # ``actor_user_id`` to NULL on user deletion) -- both surface identically as
    # a null email, and the dashboard renders "Former teammate" for either.
    actor_email: str | None
    action: str
    target_type: str
    target_id: str | None
    data: dict[str, Any]
    created_at: str


class AuditLogListOut(BaseModel):
    entries: list[AuditLogEntryOut]
    total: int
    page: int
    per_page: int


@router.get("/orgs/{org_id}/audit-log", response_model=AuditLogListOut)
async def list_audit_log(
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
    action: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1)] = audit.DEFAULT_PER_PAGE,
) -> AuditLogListOut:
    result = await audit.list_audit_log(
        ctx.org_id,
        action=action,
        page=audit.clamp_page(page),
        per_page=audit.clamp_per_page(per_page),
    )
    # ``actor_user_id`` comes back from the raw query as a ``uuid.UUID`` (or
    # None), matching ``load_users_by_ids``'s ``{uuid.UUID: email}`` key type.
    actor_ids = [
        entry["actor_user_id"]
        for entry in result["entries"]
        if entry["actor_user_id"] is not None
    ]
    emails = await accounts.load_users_by_ids(actor_ids)
    entries = [
        AuditLogEntryOut(
            id=str(entry["id"]),
            actor_user_id=(
                str(entry["actor_user_id"])
                if entry["actor_user_id"] is not None
                else None
            ),
            actor_email=(
                emails.get(entry["actor_user_id"])
                if entry["actor_user_id"] is not None
                else None
            ),
            action=entry["action"],
            target_type=entry["target_type"],
            target_id=entry["target_id"],
            data=entry["data"] or {},
            created_at=entry["created_at"].isoformat(),
        )
        for entry in result["entries"]
    ]
    return AuditLogListOut(
        entries=entries,
        total=result["total"],
        page=result["page"],
        per_page=result["per_page"],
    )

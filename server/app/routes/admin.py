"""Instance-admin panel endpoints: the self-hoster's read-only operator views.

Mounted without an /api prefix (the reverse proxy strips /api), so
GET /api/admin/overview from the browser reaches /admin/overview here.

INSTANCE-ADMIN ONLY: every endpoint depends on ``require_instance_admin`` (the
instance-wide ``users.is_instance_admin`` flag, NOT an org role), so a caller
who is not an instance administrator gets a uniform 403 with the message
"Instance administrator access is required.". These are cross-tenant operator
views; the reads run on the read-only, BYPASSRLS ``crashlens_admin`` role (see
``app/admin.py`` and ``app/db.py: admin_session``) and are never reachable from
a tenant request path. The ONLY write is the instance-admin toggle, which runs
on the normal app role against the RLS-exempt ``users`` table.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app import admin
from app.auth import require_instance_admin
from app.models.schema import User

router = APIRouter(prefix="/admin", tags=["admin"])


# --- Response models ----------------------------------------------------------
class PartitionOut(BaseModel):
    name: str
    # Planner row ESTIMATE (pg_class.reltuples), not an exact count: an exact
    # count over event partitions could scan millions of rows.
    row_estimate: int


class OverviewOut(BaseModel):
    users_count: int
    orgs_count: int
    projects_count: int
    issues_count: int
    events_last_24h: int
    # None when Redis is unreachable (redis_ok is then false).
    queue_depth: int | None
    partitions: list[PartitionOut]
    db_ok: bool
    redis_ok: bool


class AdminOrgOut(BaseModel):
    id: str
    name: str
    slug: str
    created_at: str
    member_count: int
    project_count: int


class AdminOrgListOut(BaseModel):
    orgs: list[AdminOrgOut]
    total: int
    page: int
    per_page: int


class AdminUserOut(BaseModel):
    id: str
    email: str
    created_at: str
    is_instance_admin: bool
    last_login_at: str | None


class AdminUserListOut(BaseModel):
    users: list[AdminUserOut]
    total: int
    page: int
    per_page: int


class InstanceAdminToggleRequest(BaseModel):
    enabled: bool


class InstanceAdminToggleResponse(BaseModel):
    id: str
    email: str
    is_instance_admin: bool


@router.get("/overview", response_model=OverviewOut)
async def overview(
    request: Request,
    _admin: Annotated[User, Depends(require_instance_admin)],
) -> OverviewOut:
    settings = request.app.state.settings
    data = await admin.get_overview(settings.database_url, settings.redis_url)
    return OverviewOut(
        users_count=data["users_count"],
        orgs_count=data["orgs_count"],
        projects_count=data["projects_count"],
        issues_count=data["issues_count"],
        events_last_24h=data["events_last_24h"],
        queue_depth=data["queue_depth"],
        partitions=[PartitionOut(**p) for p in data["partitions"]],
        db_ok=data["db_ok"],
        redis_ok=data["redis_ok"],
    )


@router.get("/orgs", response_model=AdminOrgListOut)
async def list_orgs(
    _admin: Annotated[User, Depends(require_instance_admin)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1)] = admin.DEFAULT_PER_PAGE,
) -> AdminOrgListOut:
    result = await admin.list_orgs(admin.clamp_page(page), admin.clamp_per_page(per_page))
    return AdminOrgListOut(
        orgs=[
            AdminOrgOut(
                id=row["id"],
                name=row["name"],
                slug=row["slug"],
                created_at=row["created_at"].isoformat(),
                member_count=row["member_count"],
                project_count=row["project_count"],
            )
            for row in result["orgs"]
        ],
        total=result["total"],
        page=result["page"],
        per_page=result["per_page"],
    )


@router.get("/users", response_model=AdminUserListOut)
async def list_users(
    _admin: Annotated[User, Depends(require_instance_admin)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1)] = admin.DEFAULT_PER_PAGE,
) -> AdminUserListOut:
    result = await admin.list_users(admin.clamp_page(page), admin.clamp_per_page(per_page))
    return AdminUserListOut(
        users=[
            AdminUserOut(
                id=row["id"],
                email=row["email"],
                created_at=row["created_at"].isoformat(),
                is_instance_admin=row["is_instance_admin"],
                last_login_at=(
                    row["last_login_at"].isoformat()
                    if row["last_login_at"] is not None
                    else None
                ),
            )
            for row in result["users"]
        ],
        total=result["total"],
        page=result["page"],
        per_page=result["per_page"],
    )


@router.post("/users/{user_id}/instance-admin", response_model=InstanceAdminToggleResponse)
async def set_instance_admin(
    user_id: str,
    body: InstanceAdminToggleRequest,
    admin_user: Annotated[User, Depends(require_instance_admin)],
) -> InstanceAdminToggleResponse:
    try:
        result = await admin.set_instance_admin(
            user_id, body.enabled, str(admin_user.id)
        )
    except admin.LastAdminError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except admin.NoSuchUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such user."
        ) from exc
    return InstanceAdminToggleResponse(
        id=result["id"],
        email=result["email"],
        is_instance_admin=result["is_instance_admin"],
    )

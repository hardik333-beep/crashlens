"""Project, DSN-key, and member management endpoints.

Mounted without an /api prefix (the reverse proxy strips /api), so
POST /api/orgs/{org_id}/projects from the browser reaches
/orgs/{org_id}/projects here.

Every handler depends on ``require_org_member`` or ``require_org_admin``, so the
org id in the path is verified against the caller's membership BEFORE any read or
write. The verified ``ctx.org_id`` is then the only org id passed to the service
layer, which scopes all DML through ``tenant_session`` (RLS). No handler trusts a
path org id on its own, and none writes ``WHERE org_id = ...`` by hand.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import projects
from app.auth import OrgContext, require_org_admin, require_org_member

router = APIRouter(tags=["projects"])


# --- Response / request models -----------------------------------------------
class ProjectOut(BaseModel):
    id: str
    name: str
    slug: str
    platform: str | None
    created_at: str


class DsnKeyOut(BaseModel):
    id: str
    public_key: str
    status: str
    created_at: str


class ProjectDetailOut(ProjectOut):
    keys: list[DsnKeyOut]


class MemberOut(BaseModel):
    user_id: str
    email: str
    role: str


class CreateProjectRequest(BaseModel):
    name: str
    platform: str | None = None


_PROJECT_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Project not found.",
)


def _project_out(project: dict) -> ProjectOut:
    return ProjectOut(
        id=str(project["id"]),
        name=project["name"],
        slug=project["slug"],
        platform=project["platform"],
        created_at=project["created_at"].isoformat(),
    )


def _key_out(key: dict) -> DsnKeyOut:
    return DsnKeyOut(
        id=str(key["id"]),
        public_key=key["public_key"],
        status=key["status"],
        created_at=key["created_at"].isoformat(),
    )


# --- Projects -----------------------------------------------------------------
@router.get("/orgs/{org_id}/projects", response_model=list[ProjectOut])
async def list_projects(
    ctx: Annotated[OrgContext, Depends(require_org_member)],
) -> list[ProjectOut]:
    rows = await projects.list_projects(ctx.org_id)
    return [_project_out(row) for row in rows]


@router.post(
    "/orgs/{org_id}/projects",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectOut,
)
async def create_project(
    body: CreateProjectRequest,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> ProjectOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A project name is required.",
        )
    platform = body.platform.strip() if body.platform else None
    project = await projects.create_project(ctx.org_id, name, platform or None)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A project with a conflicting identifier already exists.",
        )
    return _project_out(project)


@router.get(
    "/orgs/{org_id}/projects/{project_id}", response_model=ProjectDetailOut
)
async def get_project(
    project_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_member)],
) -> ProjectDetailOut:
    project = await projects.get_project(ctx.org_id, project_id)
    if project is None:
        raise _PROJECT_NOT_FOUND
    base = _project_out(project)
    return ProjectDetailOut(
        **base.model_dump(),
        keys=[_key_out(key) for key in project["keys"]],
    )


@router.delete(
    "/orgs/{org_id}/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_project(
    project_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> None:
    deleted = await projects.delete_project(ctx.org_id, project_id)
    if not deleted:
        raise _PROJECT_NOT_FOUND


# --- DSN keys -----------------------------------------------------------------
@router.post(
    "/orgs/{org_id}/projects/{project_id}/keys",
    status_code=status.HTTP_201_CREATED,
    response_model=DsnKeyOut,
)
async def create_key(
    project_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> DsnKeyOut:
    key = await projects.create_dsn_key(ctx.org_id, project_id)
    if key is None:
        raise _PROJECT_NOT_FOUND
    return _key_out(key)


@router.post(
    "/orgs/{org_id}/projects/{project_id}/keys/{key_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_key(
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> None:
    revoked = await projects.revoke_dsn_key(ctx.org_id, project_id, key_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Key not found or already revoked.",
        )


# --- Members ------------------------------------------------------------------
@router.get("/orgs/{org_id}/members", response_model=list[MemberOut])
async def list_members(
    ctx: Annotated[OrgContext, Depends(require_org_member)],
) -> list[MemberOut]:
    members = await projects.list_members(ctx.org_id)
    return [
        MemberOut(
            user_id=str(m["user_id"]),
            email=m["email"],
            role=m["role"],
        )
        for m in members
    ]

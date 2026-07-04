"""Organization management endpoints. Currently: creating member invites.

Mounted without an /api prefix (the proxy strips /api). The single endpoint here
is admin only and goes through ``require_org_admin``, so the org id in the path
is verified against the caller's membership before any write happens.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import accounts
from app.auth import OrgContext, require_org_admin

router = APIRouter(tags=["orgs"])

_VALID_ROLES = ("admin", "member")


class CreateInviteRequest(BaseModel):
    email: str
    role: str


class InviteOut(BaseModel):
    id: str
    email: str
    role: str
    expires_at: str


class CreateInviteResponse(BaseModel):
    invite: InviteOut
    # The raw invite token, returned exactly ONCE. Delivering it to the invitee
    # (email) is the alerts slice's job; the API surfaces it here and never again.
    token: str


@router.post(
    "/orgs/{org_id}/invites",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateInviteResponse,
)
async def create_invite(
    body: CreateInviteRequest,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> CreateInviteResponse:
    if body.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"role must be one of {_VALID_ROLES}.",
        )
    # ctx.org_id is the VERIFIED org id (require_org_admin confirmed the caller is
    # an admin of it); it is safe to scope the write. ctx.user.id is the verified
    # actor recorded on the "member.invited" audit row.
    invite, raw_token = await accounts.create_invite(
        ctx.org_id, body.email, body.role, actor_user_id=ctx.user.id
    )
    return CreateInviteResponse(
        invite=InviteOut(
            id=str(invite["id"]),
            email=invite["email"],
            role=invite["role"],
            expires_at=invite["expires_at"].isoformat(),
        ),
        token=raw_token,
    )

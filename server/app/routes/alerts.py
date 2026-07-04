"""Alert-channel management endpoints.

Mounted without an /api prefix (the reverse proxy strips /api), so
GET /api/orgs/{org_id}/alert-channels from the browser reaches
/orgs/{org_id}/alert-channels here.

Reads require membership (``require_org_member``); every write requires admin
(``require_org_admin``). The org id in the path is verified against the caller's
membership BEFORE any read or write, and only the verified ``ctx.org_id`` reaches
the service layer, which scopes all DML through ``tenant_session`` (RLS).

SECRETS: a channel's Slack/webhook URL can embed a token. GET NEVER returns it in
full -- ``alerts.mask_target`` reduces it to scheme + host + "/...". The UI edits a
channel by REPLACING its URL, never by reading it back.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import alerts
from app.auth import OrgContext, require_org_admin, require_org_member

router = APIRouter(tags=["alerts"])


# --- Response / request models -----------------------------------------------
class AlertChannelOut(BaseModel):
    id: str
    type: str
    project_id: str | None
    enabled: bool
    # A display-safe summary of the destination; never the secret URL.
    target: str
    created_at: str


class CreateChannelRequest(BaseModel):
    type: str
    config: dict | None = None
    project_id: str | None = None


class UpdateChannelRequest(BaseModel):
    enabled: bool | None = None
    config: dict | None = None


_CHANNEL_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Alert channel not found.",
)


def _channel_out(channel: dict) -> AlertChannelOut:
    return AlertChannelOut(
        id=str(channel["id"]),
        type=channel["type"],
        project_id=str(channel["project_id"]) if channel["project_id"] else None,
        enabled=channel["enabled"],
        target=alerts.mask_target(channel["type"], channel["config"] or {}),
        created_at=channel["created_at"].isoformat(),
    )


@router.get("/orgs/{org_id}/alert-channels", response_model=list[AlertChannelOut])
async def list_alert_channels(
    ctx: Annotated[OrgContext, Depends(require_org_member)],
) -> list[AlertChannelOut]:
    rows = await alerts.list_channels(ctx.org_id)
    return [_channel_out(row) for row in rows]


@router.post(
    "/orgs/{org_id}/alert-channels",
    status_code=status.HTTP_201_CREATED,
    response_model=AlertChannelOut,
)
async def create_alert_channel(
    body: CreateChannelRequest,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> AlertChannelOut:
    project_id = _parse_project_id(body.project_id)
    try:
        channel = await alerts.create_channel(
            ctx.org_id, body.type, body.config, project_id, actor_user_id=ctx.user.id
        )
    except alerts.ChannelConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if channel is None:
        # The only None path is an unknown / other-org project scope.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The selected project was not found.",
        )
    return _channel_out(channel)


@router.patch(
    "/orgs/{org_id}/alert-channels/{channel_id}",
    response_model=AlertChannelOut,
)
async def update_alert_channel(
    channel_id: uuid.UUID,
    body: UpdateChannelRequest,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> AlertChannelOut:
    try:
        channel = await alerts.update_channel(
            ctx.org_id,
            channel_id,
            enabled=body.enabled,
            config=body.config,
            actor_user_id=ctx.user.id,
        )
    except alerts.ChannelConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if channel is None:
        raise _CHANNEL_NOT_FOUND
    return _channel_out(channel)


@router.delete(
    "/orgs/{org_id}/alert-channels/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_alert_channel(
    channel_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_org_admin)],
) -> None:
    deleted = await alerts.delete_channel(
        ctx.org_id, channel_id, actor_user_id=ctx.user.id
    )
    if not deleted:
        raise _CHANNEL_NOT_FOUND


def _parse_project_id(value: str | None) -> uuid.UUID | None:
    """Parse an optional project-id string into a UUID, or 400 on a malformed value."""
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The project scope is not a valid id.",
        ) from exc

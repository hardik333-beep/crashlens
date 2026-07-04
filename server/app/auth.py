"""FastAPI authentication and authorization dependencies.

``get_current_user`` turns a Bearer JWT into a loaded :class:`User`.
``require_org_member`` and ``require_org_admin`` take the org id FROM THE PATH,
which is untrusted client input, and hand back a verified :class:`OrgContext`
ONLY after confirming the session user actually belongs to that org (and, for
admin, holds the admin role). Every org-scoped handler must depend on one of
them so no handler ever trusts a path org id on its own. This is the
membership-verification duty recorded in app/db.py's docstring.
"""

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import accounts, security
from app.models.schema import User

# auto_error=False so a missing header yields our own uniform 401 rather than a
# framework default, keeping the unauthenticated response shape consistent.
_bearer = HTTPBearer(auto_error=False)

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated.",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass
class OrgContext:
    """A verified org context: the caller's proven membership in ``org_id``."""

    org_id: uuid.UUID
    role: str
    user: User


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Return the user identified by a valid Bearer JWT, or raise 401.

    A missing header, a malformed or expired token, or a token whose subject no
    longer maps to a user all produce the same 401: no distinction is drawn.
    """
    if credentials is None:
        raise _UNAUTHENTICATED
    try:
        user_id = security.decode_access_token(credentials.credentials)
    except Exception as exc:  # noqa: BLE001 - any decode failure is a 401
        raise _UNAUTHENTICATED from exc

    user = await accounts.load_user_by_id(user_id)
    if user is None:
        raise _UNAUTHENTICATED
    return user


async def require_org_member(
    org_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
) -> OrgContext:
    """Return a verified :class:`OrgContext` or raise 403 for a non-member.

    ``org_id`` is a path parameter (untrusted) until the membership lookup here
    confirms the caller belongs to the org.
    """
    role = await accounts.verify_membership(user.id, org_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a member of this organization.",
        )
    return OrgContext(org_id=org_id, role=role, user=user)


async def require_instance_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Return the caller only if they are an INSTANCE administrator, else 403.

    This is the self-hoster's operator role (``users.is_instance_admin``), not
    an org role: it is instance-wide and independent of any org membership. It
    gates the instance-admin panel (cross-tenant operator stats and the
    instance-admin toggle). The 403 message is uniform, mirroring the org-admin
    dependencies above.
    """
    if not user.is_instance_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Instance administrator access is required.",
        )
    return user


async def require_org_admin(
    org_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
) -> OrgContext:
    """Return a verified admin :class:`OrgContext`, or raise 403.

    Both a non-member and a non-admin member receive an identical 403: the org
    id from the path is never trusted, and only a confirmed admin passes.
    """
    role = await accounts.verify_membership(user.id, org_id)
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access to this organization is required.",
        )
    return OrgContext(org_id=org_id, role=role, user=user)

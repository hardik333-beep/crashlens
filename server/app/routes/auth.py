"""Authentication endpoints: signup, login, current user, and invite accept.

Mounted without an /api prefix (the reverse proxy strips /api), so POST
/api/auth/login from the browser reaches /auth/login here.

ANTI-ENUMERATION CHOICES (deliberate, documented):

* Login returns one identical 401 body and status for an unknown email, a wrong
  password, and a locked account. The service layer spends Argon2 time even when
  the account is missing, so response timing does not trivially separate them.
* Signup returns a 201 of identical SHAPE whether or not the email was already
  registered. When it was, nothing is created and the returned token is inert:
  it is signed but its subject is a user id that was never persisted, so it
  cannot authenticate anything (GET /auth/me with it yields 401). This keeps the
  signup response itself indistinguishable while never handing a caller access to
  an existing account. FLAGGED for governor review: an email-verification signup
  (token issued only after the user proves control of the inbox) is the stronger
  long-term design; this slice ships the non-enumerating shape without it.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import accounts, security
from app.auth import get_current_user
from app.models.schema import User

router = APIRouter(tags=["auth"])


# --- Request / response models -----------------------------------------------
class SignupRequest(BaseModel):
    email: str
    password: str
    org_name: str


class LoginRequest(BaseModel):
    email: str
    password: str


class AcceptInviteRequest(BaseModel):
    token: str
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str


class OrgOut(BaseModel):
    id: str
    name: str
    slug: str
    role: str


class SignupResponse(BaseModel):
    token: str
    user: UserOut
    org: OrgOut


class LoginResponse(BaseModel):
    token: str
    user: UserOut
    orgs: list[OrgOut]


class MeResponse(BaseModel):
    user: UserOut
    orgs: list[OrgOut]
    # Instance-wide operator flag (NOT an org role): true only for a self-hoster
    # who administers the whole instance. The dashboard shows the instance-admin
    # nav and pages only when this is true; the API still enforces it server-side.
    is_instance_admin: bool


class AcceptInviteResponse(BaseModel):
    token: str
    user: UserOut
    org_id: str
    role: str


_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)
_INVALID_INVITE = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="This invite is invalid, expired, or already used.",
)


@router.post("/auth/signup", status_code=status.HTTP_201_CREATED, response_model=SignupResponse)
async def signup(body: SignupRequest) -> SignupResponse:
    # Password policy is about the submitted password, not the email, so a 400
    # here reveals nothing about which emails exist.
    policy_error = security.validate_password(body.password)
    if policy_error is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=policy_error)

    created = await accounts.signup(body.email, body.password, body.org_name)
    if created is None:
        # Existing email (or a rare slug race): emit an identically shaped 201
        # with an inert token and non-persisted ids. Nothing was created.
        placeholder_user = uuid.uuid4()
        placeholder_org = uuid.uuid4()
        return SignupResponse(
            token=security.create_access_token(placeholder_user),
            user=UserOut(id=str(placeholder_user), email=body.email),
            org=OrgOut(
                id=str(placeholder_org),
                name=body.org_name,
                slug=accounts._make_org_slug(body.org_name),
                role="admin",
            ),
        )

    return SignupResponse(
        token=security.create_access_token(created["user_id"]),
        user=UserOut(id=str(created["user_id"]), email=created["email"]),
        org=OrgOut(
            id=str(created["org_id"]),
            name=created["org_name"],
            slug=created["slug"],
            role=created["role"],
        ),
    )


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    user = await accounts.authenticate(body.email, body.password)
    if user is None:
        # Identical response for unknown email, wrong password, and locked.
        raise _INVALID_CREDENTIALS
    orgs = await accounts.list_user_orgs(user.id)
    return LoginResponse(
        token=security.create_access_token(user.id),
        user=UserOut(id=str(user.id), email=user.email),
        orgs=[OrgOut(**o) for o in orgs],
    )


@router.get("/auth/me", response_model=MeResponse)
async def me(user: Annotated[User, Depends(get_current_user)]) -> MeResponse:
    orgs = await accounts.list_user_orgs(user.id)
    return MeResponse(
        user=UserOut(id=str(user.id), email=user.email),
        orgs=[OrgOut(**o) for o in orgs],
        is_instance_admin=user.is_instance_admin,
    )


@router.post(
    "/auth/invites/accept",
    status_code=status.HTTP_201_CREATED,
    response_model=AcceptInviteResponse,
)
async def accept_invite(body: AcceptInviteRequest) -> AcceptInviteResponse:
    try:
        result = await accounts.accept_invite(body.token, body.email, body.password)
    except security.PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if result is None:
        raise _INVALID_INVITE
    return AcceptInviteResponse(
        token=security.create_access_token(result["user_id"]),
        user=UserOut(id=str(result["user_id"]), email=result["email"]),
        org_id=str(result["org_id"]),
        role=result["role"],
    )

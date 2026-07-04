"""Unit tests for the auth crypto and policy primitives (no database).

Cover the JWT round trip and expiry rejection, Argon2 hashing, the common
password policy, invite token hashing, and the unauthenticated 401 responses
that short circuit before any database access.
"""

import datetime
import uuid

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app import security
from app.main import create_app

_SECRET = "unit-test-secret-key-long-enough-for-hs256-32b"


# --- JWT ----------------------------------------------------------------------
def test_jwt_roundtrip_returns_same_user_id() -> None:
    user_id = uuid.uuid4()
    token = security.create_access_token(user_id, secret=_SECRET)
    assert security.decode_access_token(token, secret=_SECRET) == user_id


def test_jwt_expired_token_is_rejected() -> None:
    user_id = uuid.uuid4()
    long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=8)
    token = security.create_access_token(user_id, now=long_ago, secret=_SECRET)
    with pytest.raises(jwt.ExpiredSignatureError):
        security.decode_access_token(token, secret=_SECRET)


def test_jwt_wrong_secret_is_rejected() -> None:
    token = security.create_access_token(uuid.uuid4(), secret=_SECRET)
    with pytest.raises(jwt.InvalidSignatureError):
        security.decode_access_token(token, secret="a-different-secret")


def test_jwt_carries_sub_iat_exp_claims() -> None:
    user_id = uuid.uuid4()
    token = security.create_access_token(user_id, secret=_SECRET)
    payload = jwt.decode(token, _SECRET, algorithms=["HS256"])
    assert payload["sub"] == str(user_id)
    assert payload["exp"] - payload["iat"] == int(
        security.ACCESS_TOKEN_TTL.total_seconds()
    )


# --- Argon2 -------------------------------------------------------------------
def test_argon2_hash_verifies_correct_password() -> None:
    hashed = security.hash_password("a-strong-passphrase")
    assert hashed != "a-strong-passphrase"
    assert security.verify_password(hashed, "a-strong-passphrase") is True


def test_argon2_rejects_wrong_password() -> None:
    hashed = security.hash_password("a-strong-passphrase")
    assert security.verify_password(hashed, "not-the-password") is False


def test_argon2_rejects_malformed_hash_without_raising() -> None:
    assert security.verify_password("not-a-real-hash", "whatever") is False


# --- Password policy ----------------------------------------------------------
def test_policy_rejects_short_password() -> None:
    assert security.validate_password("short") is not None


def test_policy_rejects_common_password() -> None:
    # Long enough to pass the length gate but on the common list.
    assert security.validate_password("password123") is not None
    assert security.validate_password("qwertyuiop") is not None


def test_policy_accepts_a_strong_password() -> None:
    assert security.validate_password("correct-horse-battery-staple") is None


# --- Invite token hashing -----------------------------------------------------
def test_invite_token_hash_is_deterministic_and_not_the_raw_token() -> None:
    raw = security.generate_invite_token()
    first = security.hash_invite_token(raw)
    second = security.hash_invite_token(raw)
    assert first == second
    assert first != raw
    assert len(first) == 64  # sha256 hex


def test_invite_tokens_are_unique_and_hash_differently() -> None:
    raw_a = security.generate_invite_token()
    raw_b = security.generate_invite_token()
    assert raw_a != raw_b
    assert security.hash_invite_token(raw_a) != security.hash_invite_token(raw_b)


# --- Unauthenticated access short circuits before the database ----------------
async def test_me_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/auth/me")
    assert response.status_code == 401


async def test_me_with_malformed_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/auth/me", headers={"Authorization": "Bearer not.a.jwt"}
        )
    assert response.status_code == 401

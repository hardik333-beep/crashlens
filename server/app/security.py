"""Cryptographic primitives and account-security policy for the auth slice.

This module is deliberately free of database and framework concerns so it can be
unit tested without a live Postgres or an ASGI app. It owns four things:

1. Password hashing with Argon2 (argon2-cffi).
2. Stateless session tokens: JWT HS256 signed with ``SECRET_KEY``, 7 day expiry,
   claims ``sub`` (user id) plus ``iat`` / ``exp``. Sent as ``Authorization:
   Bearer``. This is the token the dashboard fetches once and injects into
   ``localStorage`` for the Playwright login pattern.
3. Password policy: a minimum length plus a rejection list of the most common
   passwords, checked entirely in process (no external breach service).
4. Invite tokens: a random url-safe secret whose SHA-256 hash is the only thing
   ever persisted; the raw secret is shown to the caller exactly once.

SECRETS HYGIENE: nothing here logs a password, a hash, or a token. The signing
key is read from settings (environment), never hardcoded.

FLAGGED PRODUCT DEFAULTS (governor review, not final): token TTL 7 days, lockout
threshold 10 consecutive failures, lockout window 15 minutes, invite TTL 7 days,
password minimum length 10 characters. All are collected as named constants here
so they are trivial to retune.
"""

import datetime
import hashlib
import secrets
import uuid

import argon2
import jwt
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

# --- Tunable account-security policy (FLAGGED product defaults) ---------------
ACCESS_TOKEN_TTL = datetime.timedelta(days=7)
LOCKOUT_THRESHOLD = 10
LOCKOUT_DURATION = datetime.timedelta(minutes=15)
INVITE_TTL = datetime.timedelta(days=7)
PASSWORD_MIN_LENGTH = 10

_JWT_ALGORITHM = "HS256"

# Argon2 hasher with the library's calibrated defaults. One shared instance: it
# is stateless and thread safe.
_HASHER = argon2.PasswordHasher()

# A fixed hash of a throwaway string. Verifying against it lets the login path
# spend Argon2 time even when the account does not exist, so response timing does
# not trivially separate "unknown email" from "wrong password".
_DUMMY_HASH = _HASHER.hash("crashlens-timing-equalizer-not-a-real-password")

# The ~100 most common passwords (lowercased). Membership is a hard reject
# regardless of length. Sourced from widely published breach-frequency lists;
# kept in process so signup never depends on an external service.
COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "123456", "123456789", "12345678", "1234567890", "1234567",
        "password", "password1", "password123", "passw0rd", "password12",
        "qwerty", "qwerty123", "qwertyuiop", "qwerty1", "1q2w3e4r",
        "1qaz2wsx", "zaq12wsx", "qazwsx", "qwe123", "123qwe",
        "111111", "1111111", "11111111", "000000", "0000000",
        "123123", "123321", "112233", "121212", "101010",
        "abc123", "abcd1234", "abc12345", "a1b2c3d4", "aa123456",
        "iloveyou", "iloveyou1", "sunshine", "princess", "football",
        "football1", "baseball", "welcome", "welcome1", "welcome123",
        "admin", "admin123", "administrator", "root", "toor",
        "letmein", "letmein1", "login", "monkey", "monkey123",
        "dragon", "dragon123", "master", "master123", "superman",
        "batman", "trustno1", "whatever", "shadow", "michael",
        "jennifer", "jordan23", "hunter2", "starwars", "computer",
        "internet", "samsung", "google", "chocolate", "cheese",
        "asdfghjkl", "asdfghjk", "asdf1234", "zxcvbnm", "qwertzuiop",
        "123456a", "123456789a", "a123456789", "test1234", "test123456",
        "changeme", "changeme123", "default", "secret", "secret123",
        "passer2009", "michael1", "charlie", "freedom", "ninja1234",
        "harley", "ranger", "buster", "soccer", "hockey",
        "killer", "george", "sexy", "andrew", "thomas",
        "robert", "daniel", "matthew", "jessica", "loveme",
        "password!", "p@ssw0rd", "p@ssword", "qwerty12345", "1234512345",
    }
)


class PasswordPolicyError(ValueError):
    """Raised when a proposed password fails the minimum policy."""


def _utcnow() -> datetime.datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.datetime.now(datetime.UTC)


# --- Password hashing ---------------------------------------------------------
def hash_password(password: str) -> str:
    """Return an Argon2 hash for ``password``. Never logged."""
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Return True if ``password`` matches ``password_hash``; False otherwise.

    A malformed or mismatched hash returns False rather than raising, so callers
    have a single boolean contract and never leak the reason.
    """
    try:
        _HASHER.verify(password_hash, password)
        return True
    except (VerificationError, InvalidHashError):
        return False


def dummy_verify(password: str) -> None:
    """Spend Argon2 time against a fixed dummy hash and discard the result.

    Called on the login path when the account does not exist (or is locked) so
    that timing does not trivially distinguish those states from a wrong
    password. The outcome is intentionally ignored.
    """
    try:
        _HASHER.verify(_DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        pass


# --- Password policy ----------------------------------------------------------
def validate_password(password: str) -> str | None:
    """Return an error message if ``password`` violates policy, else None.

    Policy: at least ``PASSWORD_MIN_LENGTH`` characters and not one of the most
    common passwords (case insensitive).
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters long."
    if password.lower() in COMMON_PASSWORDS:
        return "Password is too common; choose something less guessable."
    return None


# --- Session tokens (JWT) -----------------------------------------------------
def create_access_token(
    user_id: uuid.UUID | str,
    *,
    now: datetime.datetime | None = None,
    secret: str | None = None,
) -> str:
    """Return a signed JWT for ``user_id`` valid for ``ACCESS_TOKEN_TTL``.

    Claims: ``sub`` (stringified user id), ``iat``, ``exp``. ``now`` and
    ``secret`` are injectable for tests; production reads the key from settings.
    """
    key = secret or get_settings().secret_key
    issued = now or _utcnow()
    payload = {
        "sub": str(user_id),
        "iat": issued,
        "exp": issued + ACCESS_TOKEN_TTL,
    }
    return jwt.encode(payload, key, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str, *, secret: str | None = None) -> uuid.UUID:
    """Return the user id from a valid ``token``.

    Raises ``jwt.PyJWTError`` for a bad signature, an expired token, or missing
    required claims, and ``ValueError`` if ``sub`` is not a UUID. Callers treat
    any exception as an authentication failure.
    """
    key = secret or get_settings().secret_key
    payload = jwt.decode(
        token,
        key,
        algorithms=[_JWT_ALGORITHM],
        options={"require": ["exp", "iat", "sub"]},
    )
    return uuid.UUID(payload["sub"])


# --- Invite tokens ------------------------------------------------------------
def generate_invite_token() -> str:
    """Return a fresh random url-safe invite secret (the raw token)."""
    return secrets.token_urlsafe(32)


def hash_invite_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest of ``raw_token``.

    Only this digest is ever stored. Resolving an invite hashes the presented
    token and looks the digest up, so a database read never exposes a usable
    secret.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

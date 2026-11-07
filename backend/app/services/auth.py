"""Password hashing (argon2id) and JWT access tokens."""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import Settings

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()
_ephemeral_secret = secrets.token_urlsafe(48)
_warned = False


class InvalidToken(Exception):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _secret(settings: Settings) -> str:
    global _warned
    if settings.jwt_secret:
        return settings.jwt_secret
    if not _warned:
        logger.warning("JWT_SECRET is not set; using a random per-process secret (tokens reset on restart).")
        _warned = True
    return _ephemeral_secret


def create_access_token(user_id: uuid.UUID, settings: Settings) -> tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    expires_in = settings.jwt_expire_minutes * 60
    now = datetime.now(UTC)
    payload = {"sub": str(user_id), "iat": now, "exp": now + timedelta(seconds=expires_in)}
    return jwt.encode(payload, _secret(settings), algorithm=settings.jwt_algorithm), expires_in


def decode_access_token(token: str, settings: Settings) -> uuid.UUID:
    try:
        payload = jwt.decode(
            token, _secret(settings), algorithms=[settings.jwt_algorithm], options={"require": ["sub", "exp"]}
        )
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidToken(str(exc)) from exc

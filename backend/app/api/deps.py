"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.config import Settings, get_settings
from app.db import get_session
from app.models import User
from app.services.auth import InvalidToken, decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_current_user(
    session: SessionDep, settings: SettingsDep, token: Annotated[str | None, Depends(oauth2_scheme)]
) -> User:
    if not token:
        raise ApiError(401, "not_authenticated", "Log in and send 'Authorization: Bearer <token>'.")
    try:
        user_id = decode_access_token(token, settings)
    except InvalidToken as exc:
        raise ApiError(401, "invalid_token", "The access token is invalid or expired.") from exc
    user = await session.get(User, user_id)
    if user is None:
        raise ApiError(401, "invalid_token", "The account for this token no longer exists.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

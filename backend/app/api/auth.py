"""Accounts: register, login (JSON or OAuth2 form for Swagger's Authorize button), me."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, SessionDep, SettingsDep, rate_limited
from app.api.errors import ApiError
from app.models import User
from app.schemas.api import Credentials, TokenResponse, UserOut
from app.services.auth import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Verified against when the username is unknown, so response timing doesn't reveal which accounts exist.
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


def _token_response(user: User, settings) -> TokenResponse:
    token, expires_in = create_access_token(user.id, settings)
    return TokenResponse(access_token=token, expires_in=expires_in, user=UserOut.model_validate(user))


async def _authenticate(session, username: str, password: str) -> User:
    user = (await session.execute(select(User).where(User.username == username.lower()))).scalar_one_or_none()
    if not verify_password(user.password_hash if user else _DUMMY_HASH, password) or user is None:
        raise ApiError(401, "invalid_credentials", "Incorrect username or password.")
    return user


@router.post(
    "/register", status_code=status.HTTP_201_CREATED, response_model=TokenResponse,
    dependencies=[rate_limited("register", "register_rate_limit")],
)  # fmt: skip
async def register(body: Credentials, session: SessionDep, settings: SettingsDep) -> TokenResponse:
    user = User(username=body.username.lower(), password_hash=hash_password(body.password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(409, "username_taken", "That username is already registered.") from exc
    await session.refresh(user)
    return _token_response(user, settings)


@router.post("/login", response_model=TokenResponse, dependencies=[rate_limited("login", "login_rate_limit")])
async def login(body: Credentials, session: SessionDep, settings: SettingsDep) -> TokenResponse:
    return _token_response(await _authenticate(session, body.username, body.password), settings)


@router.post("/token", response_model=TokenResponse, dependencies=[rate_limited("login", "login_rate_limit")])
async def token(
    form: Annotated[OAuth2PasswordRequestForm, Depends()], session: SessionDep, settings: SettingsDep
) -> TokenResponse:
    """OAuth2 password flow (form-encoded); used by the Swagger UI 'Authorize' button."""
    return _token_response(await _authenticate(session, form.username, form.password), settings)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)

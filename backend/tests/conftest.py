"""Shared fixtures.

Unit tests need nothing external. API tests need PostgreSQL: they use TEST_DATABASE_URL (default:
the docker-compose database on localhost:5433, database `walkitecture_test`, created on demand)
and are skipped if it is unreachable.
"""

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.schemas.building import Building
from app.schemas.parse import ParseOptions
from app.services.dxf_parser import parse_dxf
from app.services.sample_plan import build_demo_dxf

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://walkitecture:walkitecture@localhost:5433/walkitecture_test"
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    # Rate limits off: the suite registers many users from one IP (tests/test_prod.py covers the limiter).
    # Uploads are processed before the response (tests/test_checks.py covers the background job).
    return Settings(
        _env_file=None,
        openai_api_key=None,
        database_url=TEST_DATABASE_URL,
        login_rate_limit=0,
        register_rate_limit=0,
        process_in_background=False,
    )


@pytest.fixture
def demo_building(settings: Settings) -> Building:
    return parse_dxf(build_demo_dxf(), ParseOptions(), settings).building


async def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    conn = await asyncpg.connect(
        host=parsed.host, port=parsed.port, user=parsed.username, password=parsed.password, database="postgres",
        timeout=3,
    )  # fmt: skip
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", parsed.database)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def db_engine():
    from app.models import Base

    try:
        await _ensure_database(TEST_DATABASE_URL)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"PostgreSQL not reachable for API tests ({exc}). Run: docker compose up -d db")
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def client(db_engine, settings: Settings) -> AsyncIterator[AsyncClient]:
    from app.config import get_settings
    from app.db import get_session
    from app.main import app

    sessions = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def register(http: AsyncClient, username: str | None = None, password: str = "correct-horse-1") -> dict:
    username = username or f"user_{uuid.uuid4().hex[:10]}"
    response = await http.post("/api/auth/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def anon_client(client: AsyncClient) -> AsyncClient:
    """A client without credentials."""
    return client


@pytest.fixture
async def auth_client(client: AsyncClient) -> AsyncClient:
    """A client logged in as a fresh user (Authorization header preset)."""
    token = (await register(client))["accessToken"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client

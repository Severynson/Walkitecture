"""Login seed: idempotent account + sample plans (API tests; need PostgreSQL)."""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Plan, User
from app.services.sample_plan import build_demo_dxf
from app.services.seed import run_seed


@pytest.fixture
def sessions(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
def seed_settings(settings, tmp_path):
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "office.dxf").write_bytes(build_demo_dxf())
    (examples / "notes.txt").write_text("ignored")
    return settings.model_copy(
        update={
            "seed_username": f"Seed_{uuid.uuid4().hex[:8]}",
            "seed_password": "walkitecture-demo",
            "seed_examples_dir": str(examples),
        }
    )


async def _plans_of(sessions, username: str) -> list[Plan]:
    async with sessions() as session:
        stmt = select(Plan).join(User, Plan.owner_id == User.id).where(User.username == username)
        return list((await session.execute(stmt)).scalars())


async def test_seed_creates_a_user_with_parsed_sample_plans(sessions, seed_settings):
    report = await run_seed(sessions, seed_settings)
    assert report.user_created and report.user == seed_settings.seed_username.lower()
    assert report.plans_created == ["demo-apartment.dxf", "office.dxf"]
    plans = await _plans_of(sessions, report.user)
    assert {p.status for p in plans} == {"parsed"} and all(p.model for p in plans)


async def test_seed_is_idempotent_and_keeps_existing_password(sessions, seed_settings):
    first = await run_seed(sessions, seed_settings)
    changed = seed_settings.model_copy(update={"seed_password": "a-different-password"})
    second = await run_seed(sessions, changed)
    assert not second.user_created and second.plans_created == []
    assert sorted(second.plans_existing) == sorted(first.plans_created)
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(User).where(User.username == first.user))
    assert count == 1
    assert len(await _plans_of(sessions, first.user)) == 2


async def test_seeded_user_can_log_in_and_see_plans(anon_client, sessions, seed_settings):
    report = await run_seed(sessions, seed_settings)
    login = await anon_client.post(
        "/api/auth/login", json={"username": seed_settings.seed_username, "password": "walkitecture-demo"}
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}
    names = {p["filename"] for p in (await anon_client.get("/api/plans", headers=headers)).json()}
    assert names == set(report.plans_created)


async def test_seed_is_disabled_without_credentials(sessions, settings):
    report = await run_seed(sessions, settings.model_copy(update={"seed_username": None, "seed_password": None}))
    assert report.user is None and "skipped" in report.summary()


async def test_seed_rejects_weak_credentials(sessions, settings):
    with pytest.raises(ValueError, match="SEED_USERNAME / SEED_PASSWORD"):
        await run_seed(sessions, settings.model_copy(update={"seed_username": "demo", "seed_password": "short"}))


async def test_seed_without_plans(sessions, seed_settings):
    report = await run_seed(sessions, seed_settings.model_copy(update={"seed_sample_plans": False}))
    assert report.user_created and report.plans_created == []

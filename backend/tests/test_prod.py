"""Production pieces (ARCHITECTURE.md §9, IMPLEMENTATION_PLAN F1/C2): startup checks, SPA serving, rate limits."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import DEFAULT_SEED_PASSWORD, Settings
from app.main import create_app
from app.services.rate_limit import SlidingWindowLimiter, limiter
from tests.conftest import register

STRONG_SECRET = "x" * 40


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({}, "JWT_SECRET"),
        ({"jwt_secret": "too-short"}, "JWT_SECRET"),
        ({"jwt_secret": STRONG_SECRET, "seed_username": "demo", "seed_password": DEFAULT_SEED_PASSWORD}, "seed"),
    ],
)
def test_production_refuses_unsafe_configuration(overrides, message):
    with pytest.raises(RuntimeError, match=message):
        create_app(Settings(_env_file=None, app_env="production", **overrides))


def test_production_starts_with_a_strong_secret_and_no_seed():
    create_app(Settings(_env_file=None, app_env="production", jwt_secret=STRONG_SECRET, seed_username=None))


def test_development_does_not_require_a_secret():
    create_app(Settings(_env_file=None, app_env="development", jwt_secret=None))


@pytest.fixture
async def spa_client(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('hi')")
    (tmp_path.parent / "secret.txt").write_text("outside the static root")
    app = create_app(Settings(_env_file=None, static_dir=str(tmp_path), cors_origins=[]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_spa_files_and_client_side_routes(spa_client):
    assert "id=root" in (await spa_client.get("/")).text
    assert "id=root" in (await spa_client.get("/plans/123/view")).text  # client-side route -> index.html
    assert (await spa_client.get("/assets/app.js")).text == "console.log('hi')"
    traversal = await spa_client.get("/..%2Fsecret.txt")
    assert "outside the static root" not in traversal.text


async def test_spa_keeps_api_and_docs(spa_client):
    missing = await spa_client.get("/api/does-not-exist")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert (await spa_client.get("/openapi.json")).json()["info"]["title"] == "Walkitecture API"
    assert "swagger" in (await spa_client.get("/docs")).text.lower()


async def test_without_a_built_spa_root_redirects_to_docs(tmp_path):
    app = create_app(Settings(_env_file=None, static_dir=str(tmp_path / "missing")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 307 and response.headers["location"] == "/docs"


def test_sliding_window_limiter():
    window = SlidingWindowLimiter()
    assert [window.hit("ip", 2, now=t) for t in (0, 1)] == [None, None]
    assert window.hit("ip", 2, now=2) == pytest.approx(58)  # third attempt within the minute
    assert window.hit("other-ip", 2, now=2) is None  # keys are independent
    assert window.hit("ip", 2, now=61) is None  # the first attempt has left the window


async def test_login_is_rate_limited_per_ip(client, settings):
    from app.config import get_settings
    from app.main import app

    await register(client, "limited_user", "right-password")
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={"login_rate_limit": 3})
    limiter.reset()
    try:
        body = {"username": "limited_user", "password": "wrong-password"}
        codes = [(await client.post("/api/auth/login", json=body)).status_code for _ in range(4)]
        assert codes == [401, 401, 401, 429]
        blocked = await client.post("/api/auth/login", json={**body, "password": "right-password"})
        assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
        assert blocked.json()["error"]["code"] == "rate_limited"
    finally:
        limiter.reset()

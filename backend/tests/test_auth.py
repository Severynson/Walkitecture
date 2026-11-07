"""Accounts, tokens, and per-user plan isolation (API tests; need PostgreSQL)."""

from tests.conftest import register


async def test_register_login_and_me(anon_client):
    created = await register(anon_client, "Alice_01", "s3cret-pass")
    assert created["tokenType"] == "bearer" and created["user"]["username"] == "alice_01"

    login = await anon_client.post("/api/auth/login", json={"username": "ALICE_01", "password": "s3cret-pass"})
    assert login.status_code == 200
    token = login.json()["accessToken"]

    me = await anon_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["username"] == "alice_01"


async def test_oauth2_form_token_endpoint(anon_client):
    await register(anon_client, "form_user", "s3cret-pass")
    response = await anon_client.post("/api/auth/token", data={"username": "form_user", "password": "s3cret-pass"})
    assert response.status_code == 200 and response.json()["accessToken"]


async def test_duplicate_username_conflicts(anon_client):
    await register(anon_client, "dupe_user")
    response = await anon_client.post("/api/auth/register", json={"username": "DUPE_USER", "password": "another-pass"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "username_taken"


async def test_wrong_password_and_unknown_user_look_the_same(anon_client):
    await register(anon_client, "carol_99", "right-password")
    wrong = await anon_client.post("/api/auth/login", json={"username": "carol_99", "password": "wrong-password"})
    unknown = await anon_client.post("/api/auth/login", json={"username": "nobody_x", "password": "wrong-password"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_weak_credentials_are_rejected(anon_client):
    response = await anon_client.post("/api/auth/register", json={"username": "bob", "password": "short"})
    assert response.status_code == 422


async def test_plan_endpoints_require_a_token(anon_client):
    for method, path in [("get", "/api/plans"), ("post", "/api/plans/demo"), ("post", "/api/interpret-edit")]:
        response = await getattr(anon_client, method)(path, **({"json": {"text": "x"}} if method == "post" else {}))
        assert response.status_code == 401, path
    bad = await anon_client.get("/api/plans", headers={"Authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_token"


async def test_users_cannot_see_each_others_plans(anon_client):
    alice = {"Authorization": f"Bearer {(await register(anon_client))['accessToken']}"}
    bob = {"Authorization": f"Bearer {(await register(anon_client))['accessToken']}"}
    plan_id = (await anon_client.post("/api/plans/demo", headers=alice)).json()["plan"]["id"]

    assert (await anon_client.get(f"/api/plans/{plan_id}/scene", headers=bob)).status_code == 404
    assert (await anon_client.post(f"/api/plans/{plan_id}/undo", headers=bob)).status_code == 404
    assert all(p["id"] != plan_id for p in (await anon_client.get("/api/plans", headers=bob)).json())
    assert (await anon_client.get(f"/api/plans/{plan_id}/scene", headers=alice)).status_code == 200

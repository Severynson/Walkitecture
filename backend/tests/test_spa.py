"""Serving the built frontend from the API's origin (app/spa.py)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import register_error_handlers
from app.spa import mount_spa, spa_available


def _client(tmp_path) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "furniture").mkdir()
    (dist / "index.html").write_text("<html>spa</html>")
    (dist / "assets" / "index-abc.js").write_text("console.log(1)")
    (dist / "furniture" / "chair.glb").write_bytes(b"glTF")
    (tmp_path / "secret.txt").write_text("nope")

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}

    mount_spa(app, dist)
    return TestClient(app)


def test_api_routes_win_over_the_spa(tmp_path):
    assert _client(tmp_path).get("/api/health").json() == {"status": "ok"}


def test_static_files_are_served_with_cache_headers(tmp_path):
    client = _client(tmp_path)
    js = client.get("/assets/index-abc.js")
    assert js.status_code == 200 and "immutable" in js.headers["cache-control"]
    glb = client.get("/furniture/chair.glb")
    assert glb.status_code == 200 and glb.content == b"glTF" and glb.headers["cache-control"] == "no-cache"


def test_client_routes_fall_back_to_index(tmp_path):
    client = _client(tmp_path)
    for path in ("/", "/plans", "/plans/123/view"):
        response = client.get(path)
        assert response.status_code == 200 and response.text == "<html>spa</html>"


def test_unknown_api_paths_stay_json_404(tmp_path):
    response = _client(tmp_path).get("/api/does-not-exist")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


def test_path_traversal_returns_index_not_the_file(tmp_path):
    response = _client(tmp_path).get("/..%2Fsecret.txt")
    assert response.status_code == 200 and "nope" not in response.text


def test_spa_available_requires_index(tmp_path):
    assert spa_available(None) is None
    assert spa_available(str(tmp_path)) is None
    (tmp_path / "index.html").write_text("x")
    assert spa_available(str(tmp_path)) == tmp_path

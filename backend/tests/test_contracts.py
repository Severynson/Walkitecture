"""Committed contract files must match the code (ARCHITECTURE.md §12, IMPLEMENTATION_PLAN.md Phase 0)."""

import json
from pathlib import Path

import pytest

from app.main import create_app
from app.schemas.api import SceneResponse

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
FIXTURES = sorted((CONTRACTS / "fixtures").glob("*.scene.json")) if (CONTRACTS / "fixtures").is_dir() else []


@pytest.mark.skipif(not (CONTRACTS / "openapi.json").is_file(), reason="contracts/ not present")
def test_openapi_contract_is_up_to_date():
    committed = json.loads((CONTRACTS / "openapi.json").read_text(encoding="utf-8"))
    assert committed == create_app().openapi(), "Run: uv run python scripts/export_openapi.py (then npm run gen:types)"


@pytest.mark.skipif(not FIXTURES, reason="no fixtures in contracts/fixtures")
@pytest.mark.parametrize("path", FIXTURES, ids=[p.name for p in FIXTURES])
def test_scene_fixtures_match_the_response_schema(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("fixture", None)
    scene = SceneResponse.model_validate(data)
    assert scene.model.walls and scene.babylon.meshes


@pytest.mark.skipif(not (CONTRACTS / "fixtures" / "orientation.scene.json").is_file(), reason="no orientation fixture")
def test_orientation_fixture_semantics():
    """IMPLEMENTATION_PLAN A1: what the viewer must render (plan y -> Babylon z, glTF +z front, chair faces table)."""
    scene = json.loads((CONTRACTS / "fixtures" / "orientation.scene.json").read_text(encoding="utf-8"))["babylon"]
    chairs = {tuple(f["footprintCenter"]): f for f in scene["furniture"] if f["category"] == "chair"}
    wall_chair, table_chair = chairs[(1.5, 0.0, 4.6)], chairs[(3.4, 0.0, 1.5)]
    assert wall_chair["facing"] == [0.0, -1.0] and wall_chair["rotationY"] == pytest.approx(3.141593)
    assert table_chair["facing"] == [1.0, 0.0] and table_chair["rotationY"] == pytest.approx(1.570796)
    # The L's notch is at the top-right (+x, +z): no wall mesh is centred inside it.
    walls = [m for m in scene["meshes"] if m["kind"] == "wall"]
    assert all(not (3.2 < m["position"][0] < 5.8 and 3.2 < m["position"][2] < 4.8) for m in walls)

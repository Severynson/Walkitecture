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

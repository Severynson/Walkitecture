"""Write frontend fixtures: `GET /api/plans/{id}/scene`-shaped JSON for the sample plans.

The frontend viewer is developed against these files (`?fixture=<name>`), so it needs no running
backend. Rules-only pipeline (no LLM) with the guardrails' default fixes, the same one the tests use.
DWG samples need `dwg2dxf`, so run it inside the image:

    docker compose run --rm -v "$PWD/contracts:/contracts" backend \
        python scripts/export_fixtures.py /contracts/fixtures

Re-run after any change to the parser, the scene builder, or the schemas, in the same PR.
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.schemas.api import SceneResponse  # noqa: E402
from app.schemas.parse import ParseOptions  # noqa: E402
from app.services.babylon_scene import build_scene  # noqa: E402
from app.services.dwg_convert import convert_dwg_to_dxf, converter_available  # noqa: E402
from app.services.dxf_parser import parse_dxf  # noqa: E402
from app.services.guardrails import analyze, resolve  # noqa: E402
from app.services.sample_plan import build_demo_dxf, build_orientation_dxf, furniture_showroom  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TARGET = REPO / "contracts" / "fixtures"
EXAMPLE_DIRS = (Path("/examples"), REPO / "examples")
NAMESPACE = uuid.UUID("6f1d3f0e-2b7a-4c1e-9f5a-77a1c0ffee00")


def _fixture(name: str, dxf: bytes, settings: Settings) -> dict:
    result = parse_dxf(dxf, ParseOptions(), settings)
    analysis = analyze(result.building)
    checked = resolve(result.building, analysis.findings, [], {})
    building = checked.building
    plan_id = uuid.uuid5(NAMESPACE, name)
    response = SceneResponse(plan_id=plan_id, version=1, model=building, babylon=build_scene(building))
    data = response.model_dump(mode="json", by_alias=True)
    data["fixture"] = {
        "name": name,
        "warnings": result.warnings,
        "stats": result.stats,
        "checks": [f"{f.id} {f.kind}: {f.detail} -> {f.decision}" for f in checked.findings]
        + [c.summary for c in checked.changes],
    }
    return data


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TARGET
    target.mkdir(parents=True, exist_ok=True)
    settings = Settings(_env_file=None)

    # orientation: L-shaped room checking plan y -> z, the glTF +z front, and chair-faces-table (plan A1)
    sources: list[tuple[str, bytes]] = [("demo-apartment", build_demo_dxf()), ("orientation", build_orientation_dxf())]
    examples = next((d for d in EXAMPLE_DIRS if d.is_dir()), None)
    if examples and converter_available(settings):
        for dwg in sorted(examples.glob("*.dwg")):
            dxf, _warnings = asyncio.run(convert_dwg_to_dxf(dwg.read_bytes(), settings))
            sources.append((dwg.stem, dxf))
    else:
        print("Skipping DWG examples: needs /examples (or ./examples) and dwg2dxf (run inside the image).")

    fixtures = [(name, _fixture(name, dxf, settings)) for name, dxf in sources]
    # Every model of the furniture library, facing the visitor (frontend/e2e/smoke.mjs loads them all).
    showroom = furniture_showroom()
    response = SceneResponse(plan_id=uuid.uuid5(NAMESPACE, "furniture-library"), version=1, model=showroom,
                             babylon=build_scene(showroom))  # fmt: skip
    library = response.model_dump(mode="json", by_alias=True)
    library["fixture"] = {"name": "furniture-library", "warnings": [], "stats": {}, "checks": []}
    fixtures.append(("furniture-library", library))

    for name, data in fixtures:
        path = target / f"{name}.scene.json"
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        model = data["model"]
        print(
            f"Wrote {path.name}: {len(model['walls'])} walls, {len(model['rooms'])} rooms, "
            f"{len(model['furniture'])} furniture, {len(data['babylon']['meshes'])} meshes"
        )


if __name__ == "__main__":
    main()

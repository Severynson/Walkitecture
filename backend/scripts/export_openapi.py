"""Write the API contract (OpenAPI JSON) to contracts/openapi.json.

The frontend generates its TypeScript types from this file (`npm run gen:types` in frontend/).
Re-run after any change to app/schemas/*.py or the routes, in the same PR:

    uv run python scripts/export_openapi.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402

DEFAULT_TARGET = Path(__file__).resolve().parents[2] / "contracts" / "openapi.json"


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    spec = create_app().openapi()
    target.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    schemas = spec.get("components", {}).get("schemas", {})
    print(f"Wrote {target} ({len(spec.get('paths', {}))} paths, {len(schemas)} schemas)")


if __name__ == "__main__":
    main()

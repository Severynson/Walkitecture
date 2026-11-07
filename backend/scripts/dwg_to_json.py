"""Turn a DWG (or DXF) into the descriptive JSON the rest of the pipeline works from.

    uv run python scripts/dwg_to_json.py ../examples/call-center-offices.dwg            # -> <name>.json
    uv run python scripts/dwg_to_json.py plan.dwg -o out.json --summary-only

DWG input needs LibreDWG's `dwg2dxf` (built into the Docker image; DWG2DXF_PATH to override).
Prints the summary (categories, layers, blocks) and writes the full JSON (every entity).
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.services.drawing_reader import describe_drawing  # noqa: E402
from app.services.dwg_convert import convert_dwg_to_dxf, detect_format  # noqa: E402
from app.services.layers import layer_patterns  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="Output JSON path (default: <input>.json)")
    parser.add_argument("--summary-only", action="store_true", help="Omit the per-entity list")
    args = parser.parse_args()

    settings = get_settings()
    data = args.input.read_bytes()
    fmt = detect_format(args.input.name, data)
    if fmt is None:
        sys.exit(f"{args.input} is neither DWG nor DXF")
    if fmt == "dwg":
        data, warnings = asyncio.run(convert_dwg_to_dxf(data, settings))
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)

    description = describe_drawing(data, layer_patterns(settings), include_entities=not args.summary_only)
    output = args.output or args.input.with_suffix(".json")
    output.write_text(json.dumps(description, indent=2, ensure_ascii=False))

    summary = {k: description[k] for k in ("dxfVersion", "units", "extents", "summary")}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("\nlayers:")
    for layer in description["layers"]:
        print(f"  {layer['name']!r:36} {layer['entityCount']:5}  {layer['categories']}")
    print("\nblocks:")
    for block in description["blocks"]:
        print(f"  {block['name']!r:36} x{block['insertCount']:<3} {block['category']:10} size={block['size']}")
    print(f"\nwrote {output}")


if __name__ == "__main__":
    main()

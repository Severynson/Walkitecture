"""Write the demo two-bedroom apartment DXF (the reference drawing convention) to disk.

Usage: uv run python scripts/make_sample_dxf.py [output_path]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.sample_plan import DEMO_FILENAME, build_demo_dxf  # noqa: E402

if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "samples" / DEMO_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_demo_dxf())
    print(f"Wrote {target}")

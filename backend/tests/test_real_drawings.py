"""Parsing robustness on real-world drawing traits, plus end-to-end runs on /examples DWGs.

The example tests need LibreDWG's dwg2dxf (present in the Docker image) and are skipped otherwise:
    podman/docker run ... walkitecture-backend python -m pytest tests/test_real_drawings.py
"""

import asyncio
import io
from pathlib import Path

import ezdxf
import pytest
from ezdxf.enums import InsertUnits

from app.config import Settings
from app.schemas.parse import ParseOptions
from app.services.babylon_scene import build_scene
from app.services.drawing_reader import describe_drawing
from app.services.dwg_convert import convert_dwg_to_dxf, converter_available
from app.services.dxf_parser import parse_dxf
from app.services.layers import layer_patterns
from app.services.sample_plan import build_demo_dxf

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _dxf_bytes(doc) -> bytes:
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode()


def test_implausible_declared_units_are_corrected(settings):
    doc = ezdxf.new()
    doc.units = InsertUnits.Inches  # header lies: geometry is really in meters
    doc.modelspace().add_lwpolyline([(0, 0), (19, 0), (19, 10), (0, 10)], close=True, dxfattribs={"layer": "MURO"})
    result = parse_dxf(_dxf_bytes(doc), ParseOptions(), settings)
    assert result.stats["extent"] == [19.0, 10.0]
    assert any("declares inches" in w for w in result.warnings)


def test_multi_leaf_walls_become_one_wall(settings):
    """A cavity wall drawn as 4 parallel lines (0, 0.07, 0.25, 0.32) is one 0.32 m wall."""
    doc = ezdxf.new()
    doc.units = InsertUnits.Meters
    msp = doc.modelspace()
    for y in (0, 0.07, 0.25, 0.32):
        msp.add_line((0, y), (6, y), dxfattribs={"layer": "MURO"})
    result = parse_dxf(_dxf_bytes(doc), ParseOptions(), settings)
    (wall,) = result.building.walls
    assert result.stats["wallStyle"] == "double_line"
    assert wall.thickness == pytest.approx(0.32)
    assert wall.start[1] == pytest.approx(0.16)


def test_layer0_linework_is_used_when_wall_layers_are_sparse(settings):
    doc = ezdxf.new()
    doc.units = InsertUnits.Meters
    msp = doc.modelspace()
    for y in (0, 0.2):  # a real 8 m double-line wall on layer 0
        msp.add_line((0, y), (8, y))
    for y in (3, 3.2):  # a short stub on the wall layer
        msp.add_line((0, y), (0.8, y), dxfattribs={"layer": "MURO-PROY"})
    result = parse_dxf(_dxf_bytes(doc), ParseOptions(), settings)
    assert any("layer '0'" in w for w in result.warnings)
    assert max(w.length for w in result.building.walls) == pytest.approx(8.0)


def test_rooms_from_outlines_with_coincident_endpoints_and_text_labels(settings):
    doc = ezdxf.new()
    doc.units = InsertUnits.Meters
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (5, 0), (5, 4), (0, 4)], close=True, dxfattribs={"layer": "WALLS"})
    # Not flagged closed, but first == last (common in real files); label on a generic text layer.
    msp.add_lwpolyline([(0, 0), (5, 0), (5, 4), (0, 4), (0, 0)], dxfattribs={"layer": "SUPERFICIE UTIL"})
    msp.add_text("Sup.=20 m2", dxfattribs={"layer": "TEXTO", "insert": (2, 1)})
    msp.add_text("bedroom 1", dxfattribs={"layer": "TEXTO", "insert": (2, 2)})
    msp.add_lwpolyline([(50, 50), (55, 50), (55, 54), (50, 54)], close=True, dxfattribs={"layer": "SUPERFICIE UTIL"})
    result = parse_dxf(_dxf_bytes(doc), ParseOptions(), settings)
    assert [(r.name, len(r.polygon)) for r in result.building.rooms] == [("bedroom 1", 4)]  # far outline dropped


def test_furniture_blocks_become_library_models(settings):
    doc = ezdxf.new()
    doc.units = InsertUnits.Meters
    bed = doc.blocks.new("CAMA 90")
    bed.add_lwpolyline([(0, 0), (0.9, 0), (0.9, 1.9), (0, 1.9)], close=True)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True, dxfattribs={"layer": "MURO"})
    msp.add_blockref("CAMA 90", (1, 1.0), dxfattribs={"layer": "MUEBLES"})  # foot at y=1, head at north wall
    (item,) = parse_dxf(_dxf_bytes(doc), ParseOptions(), settings).building.furniture
    assert (item.category, item.model, item.matched_by) == ("bed", "bed_single_by_quaternius.glb", "block_name")
    assert item.facing == (0.0, -1.0)


def test_drawing_description_of_the_demo_plan(settings):
    description = describe_drawing(build_demo_dxf(), layer_patterns(settings))
    assert description["units"]["name"] == "millimeters"
    assert description["summary"]["byCategory"] == {"wall": 4, "window": 5, "door": 3, "room": 3, "room_label": 3}
    door = next(e for e in description["entities"] if e["category"] == "door")
    assert door["geometry"]["block"] == "DOOR_900" and "bbox" in door
    assert {b["name"]: b["category"] for b in description["blocks"]} == {"DOOR_900": "door"}


needs_examples = pytest.mark.skipif(
    not (EXAMPLES.is_dir() and converter_available(Settings(_env_file=None))),
    reason="needs /examples and dwg2dxf (run inside the Docker image)",
)


@needs_examples
@pytest.mark.parametrize(
    ("name", "min_walls", "min_windows", "min_doors", "room_names", "min_furniture"),
    [
        ("call-center-offices", 30, 10, 12, {"reception", "Kitchen", "Call center 1"}, 10),
        ("apartments-140202", 20, 5, 5, {"kitchen", "bedroom 1", "SALÓN COMEDOR"}, 12),
    ],
)
def test_example_dwgs_end_to_end(settings, name, min_walls, min_windows, min_doors, room_names, min_furniture):
    dxf, _ = asyncio.run(convert_dwg_to_dxf((EXAMPLES / f"{name}.dwg").read_bytes(), settings))
    result = parse_dxf(dxf, ParseOptions(), settings)
    stats = result.stats
    assert stats["unitFactor"] == 1.0 and stats["wallStyle"] == "double_line"
    assert stats["wallCount"] >= min_walls
    assert stats["windowCount"] >= min_windows and stats["doorCount"] >= min_doors
    assert room_names <= {r.name for r in result.building.rooms}
    assert len(result.building.furniture) >= min_furniture
    scene = build_scene(result.building)
    assert len(scene.meshes) > stats["wallCount"] and len(scene.furniture) == len(result.building.furniture)

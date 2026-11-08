import io

import ezdxf
import pytest
from ezdxf.enums import InsertUnits

from app.schemas.parse import ParseOptions
from app.services.dwg_convert import detect_format
from app.services.dxf_parser import ParseError, parse_dxf
from app.services.geometry import validate_building
from app.services.sample_plan import build_demo_dxf


def _dxf_bytes(doc) -> bytes:
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode()


def test_demo_plan_parses_to_expected_model(settings):
    result = parse_dxf(build_demo_dxf(), ParseOptions(), settings)
    b = result.building

    assert result.warnings == []
    assert result.stats["detectedUnits"] == "millimeters"
    assert result.stats["extent"] == [10.0, 8.0]
    assert len(b.walls) == 7
    assert result.stats["windowCount"] == 5 and result.stats["doorCount"] == 3
    assert {r.name for r in b.rooms} == {"Living Room", "Bedroom 1", "Bedroom 2"}
    assert validate_building(b) == []

    # Offsets are measured from each wall's start point, including walls drawn right-to-left.
    top = next(w for w in b.walls if w.start == (10.0, 8.0) and w.end == (0.0, 8.0))
    assert [(o.offset, o.width) for o in top.openings] == [(1.4, 1.2), (6.5, 1.5)]

    # Door blocks (leaf + swing arc) become 0.9 m openings on the partition.
    partition = next(w for w in b.walls if w.start == (6.0, 0.0) and w.end == (6.0, 8.0))
    assert [(o.type, o.offset, o.width, o.sill_height) for o in partition.openings] == [
        ("door", 2.5, 0.9, 0.0),
        ("door", 5.0, 0.9, 0.0),
    ]


def test_missing_units_are_guessed_with_warning(settings):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 0
    doc.modelspace().add_lwpolyline(
        [(0, 0), (5000, 0), (5000, 4000), (0, 4000)], close=True, dxfattribs={"layer": "WALLS"}
    )
    result = parse_dxf(_dxf_bytes(doc), ParseOptions(), settings)
    assert result.stats["extent"] == [5.0, 4.0]
    assert any("no units" in w for w in result.warnings)


def test_explicit_scale_overrides_file_units(settings):
    doc = ezdxf.new()
    doc.units = InsertUnits.Millimeters
    doc.modelspace().add_line((0, 0), (300, 0), dxfattribs={"layer": "WALLS"})
    result = parse_dxf(_dxf_bytes(doc), ParseOptions(scale=0.01), settings)
    assert result.building.walls[0].length == pytest.approx(3.0)


def test_falls_back_to_unclassified_lines(settings):
    doc = ezdxf.new()
    doc.units = InsertUnits.Meters
    doc.modelspace().add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True)  # layer "0"
    result = parse_dxf(_dxf_bytes(doc), ParseOptions(), settings)
    assert len(result.building.walls) == 4
    assert any("treating" in w for w in result.warnings)


def test_no_geometry_raises_parse_error(settings):
    doc = ezdxf.new()
    doc.modelspace().add_circle((0, 0), 1)
    with pytest.raises(ParseError, match="No wall geometry"):
        parse_dxf(_dxf_bytes(doc), ParseOptions(), settings)


def test_garbage_raises_parse_error(settings):
    with pytest.raises(ParseError):
        parse_dxf(b"definitely not a dxf", ParseOptions(), settings)


def test_double_line_walls_are_paired_joined_and_bridged(settings):
    """A 5 x 4 m room with 0.2 m walls; the south wall is broken by a 1.5 m window."""
    doc = ezdxf.new()
    doc.units = InsertUnits.Meters
    msp = doc.modelspace()
    walls = {"layer": "WALLS"}
    for a, b in [
        ((0, 0), (2, 0)), ((3.5, 0), (5, 0)), ((5, 0), (5, 4)), ((5, 4), (0, 4)), ((0, 4), (0, 0)),  # outer
        ((0.2, 0.2), (2, 0.2)), ((3.5, 0.2), (4.8, 0.2)), ((4.8, 0.2), (4.8, 3.8)),  # inner
        ((4.8, 3.8), (0.2, 3.8)), ((0.2, 3.8), (0.2, 0.2)),
        ((2, 0), (2, 0.2)), ((3.5, 0), (3.5, 0.2)),  # jambs
    ]:  # fmt: skip
        msp.add_line(a, b, dxfattribs=walls)
    for y in (0, 0.1, 0.2):
        msp.add_line((2, y), (3.5, y), dxfattribs={"layer": "WINDOWS"})

    result = parse_dxf(_dxf_bytes(doc), ParseOptions(wall_style="double_line"), settings)
    walls_out = result.building.walls

    assert len(walls_out) == 4
    assert all(w.thickness == pytest.approx(0.2) for w in walls_out)
    corners = {p for w in walls_out for p in (w.start, w.end)}
    assert corners == {(0.1, 0.1), (4.9, 0.1), (4.9, 3.9), (0.1, 3.9)}
    south = next(w for w in walls_out if w.openings)
    assert south.length == pytest.approx(4.8)
    (window,) = south.openings
    assert window.type == "window"
    assert window.width == pytest.approx(1.5)
    assert window.offset == pytest.approx(1.9)


def test_custom_layer_names(settings):
    doc = ezdxf.new()
    doc.units = InsertUnits.Meters
    msp = doc.modelspace()
    msp.add_line((0, 0), (6, 0), dxfattribs={"layer": "MY_WALLS"})
    msp.add_line((1, 0), (2, 0), dxfattribs={"layer": "GLASS"})
    msp.add_line((0, 5), (6, 5), dxfattribs={"layer": "IGNORED"})
    options = ParseOptions(wall_layers=["my_*"], window_layers=["GLASS"])
    result = parse_dxf(_dxf_bytes(doc), options, settings)
    assert len(result.building.walls) == 1
    assert result.building.walls[0].openings[0].width == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("a.dwg", b"AC1032\x00\x00rest", "dwg"),
        ("a.dxf", b"  0\nSECTION\n  2\nHEADER\n", "dxf"),
        ("a.bin", b"\xef\xbb\xbf0\r\nSECTION\r\n", "dxf"),
        ("a.dxf", b"AutoCAD Binary DXF\r\n\x1a\x00", "dxf"),
        ("a.png", b"\x89PNG\r\n", None),
        ("a.dwg", b"not really a dwg", None),
    ],
)
def test_detect_format(name, data, expected):
    assert detect_format(name, data) == expected


def test_a_window_filling_the_gap_up_to_the_next_wall_extends_its_wall():
    from app.schemas.building import Wall
    from app.services.dxf_parser import _extend_into_openings

    def walls():
        return [
            Wall(id="facade", start=(0, 0), end=(2, 0), thickness=0.2, height=3.0),
            Wall(id="side", start=(5, -1), end=(5, 3), thickness=0.2, height=3.0),
        ]

    glazing = [(2.0, -0.15), (4.9, -0.15), (4.9, 0.15), (2.0, 0.15)]  # 2.9 m window block beyond the facade's end
    extended = walls()
    assert _extend_into_openings(extended, [("window", glazing)]) == 1
    assert extended[0].end == (4.9, 0)
    lonely = walls()[:1]  # no wall on the far side: the window may be anything, leave the wall alone
    assert _extend_into_openings(lonely, [("window", glazing)]) == 0 and lonely[0].end == (2, 0)
    on_wall = walls()
    assert _extend_into_openings(on_wall, [("window", [(0.5, -0.1), (1.5, -0.1), (1.5, 0.1), (0.5, 0.1)])]) == 0


def test_floor_tile_lines_cannot_steal_a_partitions_face():
    """A 2 m partition (faces 0.20 m apart) next to a grid of 0.25 m tile edges 0.218 m from one face."""
    from app.services.dxf_parser import _pair_double_lines

    faces = [((0.0, 0.0), (0.0, 2.0)), ((0.2, 0.0), (0.2, 2.0))]
    tiles = [((-0.218, y), (-0.218, y + 0.25)) for y in (0.1, 0.5, 0.9, 1.3)]
    (wall,) = _pair_double_lines(faces + tiles, 0.05, 0.5, [])
    assert wall.thickness == pytest.approx(0.2) and wall.length == pytest.approx(2.0)


def test_short_piers_beside_windows_still_pair():
    from app.services.dxf_parser import _pair_double_lines

    (pier,) = _pair_double_lines([((0, 0), (0.255, 0)), ((0.07, 0.07), (0.255, 0.07))], 0.05, 0.5, [])
    assert pier.thickness == pytest.approx(0.07)

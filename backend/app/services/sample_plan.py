"""Generate the known-good demo DXF: a 10 m x 8 m two-bedroom apartment drawn in millimeters.

It follows the supported drawing convention exactly, so it doubles as a reference for anyone
preparing their own DWG/DXF files:

* WALLS   - wall centerlines (closed exterior polyline + partition lines + one free-standing wall)
* WINDOWS - one LINE per window, lying on its wall
* DOORS   - INSERTs of a door block (leaf + 90-degree swing arc), base point at the hinge
* ROOMS   - closed room outlines plus TEXT labels
"""

import io

import ezdxf
from ezdxf.enums import InsertUnits

DEMO_FILENAME = "demo-apartment.dxf"


def build_demo_dxf() -> bytes:
    doc = ezdxf.new("R2010", setup=True)
    doc.units = InsertUnits.Millimeters
    for name, color in (("WALLS", 7), ("WINDOWS", 5), ("DOORS", 3), ("ROOMS", 8)):
        doc.layers.add(name, color=color)

    door = doc.blocks.new("DOOR_900")
    door.add_line((0, 0), (0, 900))
    door.add_arc((0, 0), 900, 0, 90)

    msp = doc.modelspace()
    walls = {"layer": "WALLS"}
    msp.add_lwpolyline([(0, 0), (10000, 0), (10000, 8000), (0, 8000)], close=True, dxfattribs=walls)
    msp.add_line((6000, 0), (6000, 8000), dxfattribs=walls)  # living area | bedrooms
    msp.add_line((6000, 4000), (10000, 4000), dxfattribs=walls)  # bedroom 1 | bedroom 2
    msp.add_line((2000, 5500), (4000, 5500), dxfattribs=walls)  # free-standing partition (movable)

    windows = {"layer": "WINDOWS"}
    msp.add_line((1500, 0), (3000, 0), dxfattribs=windows)  # living room, south
    msp.add_line((7400, 0), (8600, 0), dxfattribs=windows)  # bedroom 1, south
    msp.add_line((2000, 8000), (3500, 8000), dxfattribs=windows)  # living room, north
    msp.add_line((7400, 8000), (8600, 8000), dxfattribs=windows)  # bedroom 2, north
    msp.add_line((0, 3000), (0, 4500), dxfattribs=windows)  # living room, west

    doors = {"layer": "DOORS"}
    msp.add_blockref("DOOR_900", (0, 1000), dxfattribs={**doors, "rotation": 90})  # entrance
    msp.add_blockref("DOOR_900", (6000, 2500), dxfattribs={**doors, "rotation": 90})  # bedroom 1
    msp.add_blockref("DOOR_900", (6000, 5000), dxfattribs={**doors, "rotation": 90})  # bedroom 2

    rooms = {"layer": "ROOMS"}
    for name, (x0, y0, x1, y1) in {
        "Living Room": (0, 0, 6000, 8000),
        "Bedroom 1": (6000, 0, 10000, 4000),
        "Bedroom 2": (6000, 4000, 10000, 8000),
    }.items():
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs=rooms)
        msp.add_text(name, height=250, dxfattribs={**rooms, "insert": ((x0 + x1) / 2 - 700, (y0 + y1) / 2)})

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")

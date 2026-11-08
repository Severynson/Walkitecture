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


ORIENTATION_FILENAME = "orientation.dxf"


def build_orientation_dxf() -> bytes:
    """Orientation check (IMPLEMENTATION_PLAN A1), drawn in meters.

    An asymmetric L-shaped room (6 m along x, 5 m along y; the notch is top-right) with a door on the
    south wall, a window on the west wall, one chair with its back to the north wall, and a table with
    a chair on its west side. Seen from above, un-mirrored: the notch is top-right, the lone chair faces
    south (-y), and the table chair faces east (+x) toward the table.
    """
    doc = ezdxf.new("R2010", setup=True)
    doc.units = InsertUnits.Meters
    for name in ("WALLS", "WINDOWS", "DOORS", "ROOMS", "MUEBLES"):
        doc.layers.add(name)

    door = doc.blocks.new("DOOR_900")
    door.add_line((0, 0), (0, 0.9))
    door.add_arc((0, 0), 0.9, 0, 90)
    chair = doc.blocks.new("CHAIR")
    chair.add_lwpolyline([(-0.25, -0.25), (0.25, -0.25), (0.25, 0.25), (-0.25, 0.25)], close=True)
    table = doc.blocks.new("TABLE")
    table.add_lwpolyline([(-0.6, -0.4), (0.6, -0.4), (0.6, 0.4), (-0.6, 0.4)], close=True)

    msp = doc.modelspace()
    outline = [(0, 0), (6, 0), (6, 3), (3, 3), (3, 5), (0, 5)]
    msp.add_lwpolyline(outline, close=True, dxfattribs={"layer": "WALLS"})
    msp.add_line((0, 1.5), (0, 2.7), dxfattribs={"layer": "WINDOWS"})
    msp.add_blockref("DOOR_900", (1, 0), dxfattribs={"layer": "DOORS"})
    msp.add_lwpolyline(outline, close=True, dxfattribs={"layer": "ROOMS"})
    msp.add_text("Studio", height=0.2, dxfattribs={"layer": "ROOMS", "insert": (1.2, 2.2)})
    msp.add_blockref("CHAIR", (1.5, 4.6), dxfattribs={"layer": "MUEBLES"})  # back to the north wall
    msp.add_blockref("TABLE", (4.5, 1.5), dxfattribs={"layer": "MUEBLES"})
    msp.add_blockref("CHAIR", (3.4, 1.5), dxfattribs={"layer": "MUEBLES"})  # west of the table

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


SHOWROOM_COLUMNS = 6
SHOWROOM_GAP = 0.9  # m between pieces


def furniture_showroom():
    """One room with every model of the furniture library, in rows facing the visitor (the
    `furniture-library` fixture: a visual catalog, and a browser test that every GLB loads)."""
    from app.schemas.building import Building, FurnitureItem, Opening, Room, Wall
    from app.services.furniture_catalog import CATALOG

    height = 10 * 0.3048
    cell_w = max(m.real_size[0] for m in CATALOG) + SHOWROOM_GAP
    cell_d = max(m.real_size[2] for m in CATALOG) + SHOWROOM_GAP
    rows = -(-len(CATALOG) // SHOWROOM_COLUMNS)
    width, depth = SHOWROOM_COLUMNS * cell_w + 1.0, rows * cell_d + 4.0  # 3 m walkway in front of the rows
    furniture = []
    for index, model in enumerate(CATALOG):
        row, col = divmod(index, SHOWROOM_COLUMNS)
        w, h, d = model.real_size
        x, y = 0.5 + (col + 0.5) * cell_w, depth - 0.5 - (row + 0.5) * cell_d
        furniture.append(
            FurnitureItem(
                id=f"furniture_{index + 1}", category=model.category, model=model.file, source_block=model.file,
                matched_by="manual", position=(round(x, 4), round(y, 4)),
                facing=(0.0, -1.0), width=w, depth=d, height=h,
            )
        )  # fmt: skip
    door = Opening(id="door_1", type="door", offset=width / 2 - 0.45, width=0.9, height=7 * 0.3048)
    corners = [(0.0, 0.0), (width, 0.0), (width, depth), (0.0, depth)]
    walls = [
        Wall(id=f"wall_{i + 1}", start=corners[i], end=corners[(i + 1) % 4], thickness=0.2, height=height,
             openings=[door] if i == 0 else [])
        for i in range(4)
    ]  # fmt: skip
    room = Room(id="room_1", name="Furniture library", label_position=(width / 2, 1.5))
    return Building(ceiling_height=height, walls=walls, rooms=[room], furniture=furniture)

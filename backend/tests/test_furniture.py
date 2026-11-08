"""Furniture library matching and rule-based placement (ARCHITECTURE.md module 2)."""

from pathlib import Path

import pytest

from app.schemas.building import Wall
from app.schemas.parse import FURNITURE_CATEGORIES
from app.services.babylon_scene import FRONT_ANGLE
from app.services.furniture_catalog import CATALOG, DOOR_LEAF, category_for, category_from_size, choose_model
from app.services.furniture_placement import FurnitureCandidate, place_furniture
from app.services.glb_bounds import glb_bounds

FURNITURE_DIR = Path(__file__).resolve().parents[2] / "frontend" / "public" / "furniture"


@pytest.mark.skipif(not FURNITURE_DIR.is_dir(), reason="frontend/public/furniture not present")
def test_catalog_matches_the_glb_library():
    files = {p.name for p in FURNITURE_DIR.glob("*.glb")}
    assert {m.file for m in CATALOG} | {DOOR_LEAF.file} == files
    for model in (*CATALOG, DOOR_LEAF):
        size, center = glb_bounds(FURNITURE_DIR / model.file)
        assert size == pytest.approx(model.native_size, abs=0.002), model.file
        assert center == pytest.approx(model.native_center, abs=0.002), model.file


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("CAMA180 90", "bed"), ("camamatrimnueva", "bed"), ("SILL-01Z", "chair"), ("SILLONCONPERSONA", "sofa"),
        ("ESCRITORIO JUVENIL", "desk"), ("nueva mesa comedor", "table"), ("mesareunion", "table"),
        ("SOFA CON HOMBRE", "sofa"), ("WC1", "toilet"), ("MUEBLE COMODIN", "closet"), ("Office Chair", "chair"),
        ("King Bed", "bed"), ("LAVABO1", None), ("18140101", None), ("Armchair", "sofa"),
        ("BAÑERA 170", "bathtub"), ("Bathtub", "bathtub"), ("PLATO DUCHA 90", "shower"), ("Shower", "shower"),
        ("FREGADERO 2 SENOS", "sink"), ("Kitchen Sink", "sink"), ("NEVERA", "fridge"), ("Refrigerator", "fridge"),
        ("MESITA DE NOCHE", "nightstand"), ("Bedside table", "nightstand"), ("MUEBLE COCINA", "kitchen"),
        ("Kitchen Cabinet", "kitchen"), ("CÓMODA", "dresser"), ("Chest of drawers", "dresser"),
        ("ESTANTERIA", "bookshelf"), ("Bookcase", "bookshelf"), ("FILE CABINET", "cabinet"), ("APARADOR", "cabinet"),
        ("Wardrobe", "closet"), ("mesita rincon con lampara", "table"),
    ],
)  # fmt: skip
def test_block_names_map_to_categories(name, category):
    assert category_for(name) == category


def test_every_category_has_a_model_and_every_model_a_category():
    assert {m.category for m in CATALOG} == set(FURNITURE_CATEGORIES)
    assert all(m.front in FRONT_ANGLE for m in CATALOG)
    assert len({m.file for m in CATALOG}) == len(CATALOG)


@pytest.mark.parametrize(
    ("category", "size", "name", "file"),
    [
        ("sofa", (0.9, 0.85), "SILLON", "couch_small_quarternius.glb"),
        ("sofa", (2.0, 0.9), "SOFA", "couch_medium_by_quaternius.glb"),
        ("chair", (0.6, 0.6), "SILLA OFICINA", "office_chair_quaternius.glb"),
        ("chair", (0.5, 0.55), "SILLA", "chair_by_quaternius.glb"),
        ("kitchen", (0.6, 0.6), "MUEBLE COCINA", "kitchen_cabinet_by_kenney.glb"),
        ("kitchen", (1.2, 0.35), "BARRA", "kitchen_bar_by_kenney.glb"),
        ("cabinet", (0.45, 0.6), "ARCHIVADOR", "file_cabinet_j-toastie.glb"),
        ("cabinet", (0.9, 0.45), "APARADOR", "cabinet_by_creativetrio.glb"),
    ],
)  # fmt: skip
def test_variants_follow_size_and_name(category, size, name, file):
    assert choose_model(category, *size, name=name).file == file


def test_a_chair_at_a_desk_is_an_office_chair():
    desk = FurnitureCandidate("ESCRITORIO", "MUEBLES", "desk", "block_name", square((2, 2), 1.4, 0.7))
    at_desk = FurnitureCandidate("SILLA", "MUEBLES", "chair", "block_name", square((2, 1.2), 0.5, 0.5))
    at_table = FurnitureCandidate("SILLA", "MUEBLES", "chair", "block_name", square((8, 1.2), 0.5, 0.5))
    table = FurnitureCandidate("MESA", "MUEBLES", "table", "block_name", square((8, 2), 1.6, 0.9))
    placed = {
        i.source_block + str(i.position[0] > 5): i for i in place_furniture([desk, at_desk, table, at_table], [], [])
    }
    assert placed["SILLAFalse"].model == "office_chair_quaternius.glb"
    assert placed["SILLATrue"].model == "chair_by_quaternius.glb"


def square(center, width, depth):
    (cx, cy), w, d = center, width / 2, depth / 2
    return [(cx - w, cy - d), (cx + w, cy - d), (cx + w, cy + d), (cx - w, cy + d)]


def test_size_guess_is_limited_to_generic_furniture_layers():
    assert category_from_size(0.58, 0.62, "Muebles") == "chair"
    assert category_from_size(0.69, 1.6, "Muebles") == "desk"
    assert category_from_size(0.8, 1.2, "SANITARIOS") is None  # a shower is not a desk
    assert category_from_size(0.54, 0.6, "MOB-FIJO") is None  # fixed fixtures are not chairs
    assert category_from_size(3.5, 3.5, "Muebles") is None


def test_model_variants_follow_the_footprint():
    assert choose_model("bed", 1.9, 2.0).variant == "double"
    assert choose_model("bed", 0.9, 1.9).variant == "single"
    assert choose_model("table", 1.2, 1.2, round_shape=True).variant == "round"
    assert choose_model("table", 1.17, 0.63, round_shape=True).variant == "rectangular"
    assert choose_model("sofa", 2.4, 2.3).variant == "corner"
    assert choose_model("lamp", 1, 1) is None


def _box(cx, cy, w, d):
    return [(cx - w / 2, cy - d / 2), (cx + w / 2, cy - d / 2), (cx + w / 2, cy + d / 2), (cx - w / 2, cy + d / 2)]


ROOM = [
    Wall(id="s", start=(0, 0), end=(6, 0), thickness=0.2, height=3),
    Wall(id="e", start=(6, 0), end=(6, 5), thickness=0.2, height=3),
    Wall(id="n", start=(6, 5), end=(0, 5), thickness=0.2, height=3),
    Wall(id="w", start=(0, 5), end=(0, 0), thickness=0.2, height=3),
]


def test_furniture_faces_away_from_the_wall_behind_it():
    bed = FurnitureCandidate("CAMA", "MUEBLES", "bed", "block_name", _box(3, 4.0, 1.4, 1.9))  # headboard at north
    (item,) = place_furniture([bed], ROOM, [])
    assert item.facing == (0.0, -1.0)
    assert (item.width, item.depth) == pytest.approx((1.4, 1.9))
    assert item.position == pytest.approx((3, 4.0))


def test_chairs_turn_toward_their_table():
    table = FurnitureCandidate("mesa", "MUEBLES", "table", "block_name", _box(3, 2.5, 1.6, 0.9))
    chair = FurnitureCandidate("silla", "MUEBLES", "chair", "block_name", _box(1.8, 2.5, 0.5, 0.5))
    items = place_furniture([chair, table], ROOM, [])
    by_category = {i.category: i for i in items}
    assert by_category["chair"].facing == pytest.approx((1.0, 0.0))


def test_unknown_categories_are_reported_not_placed():
    warnings: list[str] = []
    sink = FurnitureCandidate("LAVABO", "SANITARIOS", None, "layer", _box(1, 1, 0.5, 0.4))
    assert place_furniture([sink], ROOM, warnings) == []
    assert "LAVABO" in warnings[0]


# --- Corners: the back must give the footprint the model's proportions (screenshot regressions) ---


def test_wardrobe_in_a_corner_keeps_its_long_side_against_the_wall():
    # 1.6 x 0.4 m built-in wardrobe in the north-east corner: walls touch its long (north) side AND its
    # short (east) side. The back must be the long side: 1.6 wide, 0.4 deep, facing south.
    wardrobe = FurnitureCandidate("264", "0", "closet", "llm", _box(6 - 0.1 - 0.8, 5 - 0.1 - 0.2, 1.6, 0.4))
    (item,) = place_furniture([wardrobe], ROOM, [])
    assert item.facing == (0.0, -1.0)
    assert (item.width, item.depth) == pytest.approx((1.6, 0.4))


def test_wardrobe_along_a_side_wall_in_a_corner():
    # Same wardrobe turned 90 degrees (0.4 x 1.6) in the same corner: its long side is the east one.
    wardrobe = FurnitureCandidate("264", "0", "closet", "llm", _box(6 - 0.1 - 0.2, 5 - 0.1 - 0.8, 0.4, 1.6))
    (item,) = place_furniture([wardrobe], ROOM, [])
    assert item.facing == (-1.0, 0.0)
    assert (item.width, item.depth) == pytest.approx((1.6, 0.4))


def test_bed_in_a_corner_has_its_head_against_the_short_wall():
    # 1.4 x 1.9 bed in the south-west corner touching the west wall (long side) and south wall (head).
    bed = FurnitureCandidate("CAMA", "MUEBLES", "bed", "block_name", _box(0.1 + 0.7, 0.1 + 0.95, 1.4, 1.9))
    (item,) = place_furniture([bed], ROOM, [])
    assert item.facing == (0.0, 1.0)
    assert (item.width, item.depth) == pytest.approx((1.4, 1.9))


def test_free_standing_bed_faces_along_its_length():
    bed = FurnitureCandidate("CAMA", "MUEBLES", "bed", "block_name", _box(3, 2.5, 1.9, 1.4))  # drawn lying along x
    (item,) = place_furniture([bed], ROOM, [])
    assert abs(item.facing[0]) == pytest.approx(1.0)  # foot end points along the long (x) axis
    assert (item.width, item.depth) == pytest.approx((1.4, 1.9))

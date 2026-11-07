"""Furniture library matching and rule-based placement (ARCHITECTURE.md module 2)."""

from pathlib import Path

import pytest

from app.schemas.building import Wall
from app.services.furniture_catalog import CATALOG, category_for, category_from_size, choose_model
from app.services.furniture_placement import FurnitureCandidate, place_furniture
from app.services.glb_bounds import glb_bounds

FURNITURE_DIR = Path(__file__).resolve().parents[2] / "frontend" / "public" / "furniture"


@pytest.mark.skipif(not FURNITURE_DIR.is_dir(), reason="frontend/public/furniture not present")
def test_catalog_matches_the_glb_library():
    files = {p.name for p in FURNITURE_DIR.glob("*.glb")}
    assert {m.file for m in CATALOG} == files
    for model in CATALOG:
        size, center = glb_bounds(FURNITURE_DIR / model.file)
        assert size == pytest.approx(model.native_size, abs=0.002), model.file
        assert center == pytest.approx(model.native_center, abs=0.002), model.file


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("CAMA180 90", "bed"), ("camamatrimnueva", "bed"), ("SILL-01Z", "chair"), ("SILLONCONPERSONA", "chair"),
        ("ESCRITORIO JUVENIL", "desk"), ("nueva mesa comedor", "table"), ("mesareunion", "table"),
        ("SOFA CON HOMBRE", "sofa"), ("WC1", "toilet"), ("MUEBLE COMODIN", "closet"), ("Office Chair", "chair"),
        ("King Bed", "bed"), ("LAVABO1", None), ("NEVERA", None), ("18140101", None),
    ],
)  # fmt: skip
def test_block_names_map_to_categories(name, category):
    assert category_for(name) == category


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

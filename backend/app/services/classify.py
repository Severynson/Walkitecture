"""Decide what each drawing entity represents: wall, window, door, room, furniture, annotation, other.

Signals, strongest first:
1. INSERT block names (multilingual keywords: door/puerta, window/ventana, bed/cama, ...).
2. Layer names (configurable glob patterns; English AIA-style and Spanish defaults).
3. Geometry: on mixed "openings" layers (e.g. CARPINTERIA), a block that contains an ARC (a door
   swing) is a door, otherwise a window.
"""

import fnmatch
import re
from dataclasses import dataclass
from functools import lru_cache

from ezdxf.document import Drawing
from ezdxf.entities import DXFGraphic

from app.services.furniture_catalog import category_for

# A door/window name wins over these: "PUERTA COCINA" is the kitchen's door, not a kitchen unit. Other furniture
# words still win ("wardrobe door" is part of a wardrobe).
ROOM_WORD_CATEGORIES = (None, "kitchen")
# A cryptic "door-like" name with an arc is not a door on a sanitary/fixture layer: "pd" on SANITARIOS is a
# round shower tray (its curved front is the arc). Clear names ("PUERTA ...") still win anywhere.
FIXTURE_LAYER = re.compile(r"sanit|plumb|fontaner|ducha|shower|aparato|fixture", re.IGNORECASE)

DOOR_BLOCK = re.compile(r"door|puerta|porte|\btür", re.IGNORECASE)
# Cryptic abbreviations (P40, PC40, pp, pndo, NPBAÑO) only count as doors if the block has a swing arc.
WEAK_DOOR_BLOCK = re.compile(r"^p\d|^pc\d|^pp?\d*$|^pndo|^np|^pd$", re.IGNORECASE)
WINDOW_BLOCK = re.compile(r"window|ventana|fenster|fenetre|glaz|^v\d|^vent", re.IGNORECASE)
TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
ANNOTATION_TYPES = TEXT_TYPES | {"DIMENSION", "LEADER", "MULTILEADER", "TOLERANCE"}
LINEAR_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE"}

CATEGORIES = ("wall", "window", "door", "room", "room_label", "furniture", "annotation", "hatch", "other")


def polyline_points(entity: DXFGraphic) -> list[tuple[float, float]]:
    vertices = entity.vertices_in_wcs() if entity.dxftype() == "LWPOLYLINE" else entity.points_in_wcs()
    return [(v.x, v.y) for v in vertices]


def is_closed_shape(entity: DXFGraphic) -> bool:
    """Closed flag set, or first and last vertex coincide (common in real drawings)."""
    if entity.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
        return False
    if entity.is_closed:
        return True
    points = polyline_points(entity)
    return len(points) >= 4 and abs(points[0][0] - points[-1][0]) < 1e-6 and abs(points[0][1] - points[-1][1]) < 1e-6


@dataclass(frozen=True)
class LayerPatterns:
    wall: tuple[str, ...]
    window: tuple[str, ...]
    door: tuple[str, ...]
    opening: tuple[str, ...]  # mixed doors + windows; the block decides which
    room: tuple[str, ...]
    furniture: tuple[str, ...]
    # (BLOCK NAME upper-cased, role) pairs from ParseOptions.block_overrides; checked before any rule.
    block_overrides: tuple[tuple[str, str], ...] = ()

    def match(self, layer: str) -> str | None:
        name = layer.upper()
        for kind in ("wall", "window", "door", "opening", "room", "furniture"):
            if any(fnmatch.fnmatchcase(name, p.upper()) for p in getattr(self, kind)):
                return kind
        return None


@dataclass(frozen=True)
class Classification:
    category: str  # one of CATEGORIES
    reason: str
    furniture_category: str | None = None  # bed, chair, sofa, desk, table, closet, toilet (None = unknown)
    matched_by: str = "block_name"  # block_name | layer | manual (block override)
    explode: bool = True  # False: an ignored block's contents must not be reclassified either


class DrawingClassifier:
    def __init__(self, doc: Drawing, patterns: LayerPatterns):
        self.doc = doc
        self.patterns = patterns
        self._block_has_arc = lru_cache(maxsize=None)(self._compute_block_has_arc)
        self._overrides = dict(patterns.block_overrides)

    def classify(self, entity: DXFGraphic, layer: str | None = None) -> Classification:
        layer = layer if layer is not None else entity.dxf.get("layer", "0")
        dxftype = entity.dxftype()
        layer_kind = self.patterns.match(layer)

        if dxftype == "INSERT":
            return self._classify_insert(entity, layer, layer_kind)
        if layer_kind == "wall" and dxftype in LINEAR_TYPES:
            return Classification("wall", f"line on wall layer {layer!r}")
        if layer_kind in ("window", "door") and dxftype not in ANNOTATION_TYPES:
            return Classification(layer_kind, f"{dxftype} on {layer_kind} layer {layer!r}")
        if layer_kind == "room":
            if dxftype in TEXT_TYPES:
                return Classification("room_label", f"text on room layer {layer!r}")
            if dxftype in ("LWPOLYLINE", "POLYLINE") and is_closed_shape(entity):
                return Classification("room", f"closed polyline on room layer {layer!r}")
        if dxftype in ANNOTATION_TYPES:
            return Classification("annotation", dxftype.lower())
        if layer_kind == "furniture":
            return Classification("furniture", f"{dxftype} on furniture layer {layer!r} (loose linework)")
        if dxftype == "HATCH":
            return Classification("hatch", "fill pattern")
        return Classification("other", f"{dxftype} on unclassified layer {layer!r}")

    def _classify_insert(self, entity: DXFGraphic, layer: str, layer_kind: str | None) -> Classification:
        name = entity.dxf.get("name", "")
        override = self._overrides.get(name.upper())
        if override == "ignore":
            return Classification("other", f"block {name!r} ignored by override", explode=False)
        if override in ("door", "window"):
            return Classification(override, f"block {name!r} overridden as {override}", matched_by="manual")
        if override:
            return Classification("furniture", f"block {name!r} overridden as {override}", override, "manual")
        if layer_kind in ("window", "door"):
            return Classification(layer_kind, f"block {name!r} on {layer_kind} layer {layer!r}")
        if WINDOW_BLOCK.search(name) and category_for(name) in ROOM_WORD_CATEGORIES:
            return Classification("window", f"block name {name!r} looks like a window")
        if DOOR_BLOCK.search(name) and category_for(name) in ROOM_WORD_CATEGORIES:
            return Classification("door", f"block name {name!r} looks like a door")
        if WEAK_DOOR_BLOCK.search(name) and not FIXTURE_LAYER.search(layer) and self._block_has_arc(name):
            return Classification("door", f"block {name!r} has a door-like name and a swing arc")
        if layer_kind == "opening":
            if self._block_has_arc(name):
                return Classification("door", f"block {name!r} on openings layer {layer!r} has a swing arc")
            return Classification("window", f"block {name!r} on openings layer {layer!r} has no swing arc")
        furniture = category_for(name)
        if furniture:
            return Classification("furniture", f"block name {name!r} matches {furniture!r}", furniture)
        if layer_kind == "furniture":
            return Classification(
                "furniture", f"block {name!r} on furniture layer {layer!r}", category_for(layer) or None, "layer"
            )
        if layer_kind == "wall":
            return Classification("wall", f"block {name!r} on wall layer {layer!r}")
        return Classification("other", f"block {name!r} on unclassified layer {layer!r}")

    def _compute_block_has_arc(self, name: str, depth: int = 0) -> bool:
        block = self.doc.blocks.get(name)
        if block is None or depth > 8:
            return False
        for e in block:
            if e.dxftype() in ("ARC", "ELLIPSE"):
                return True
            if e.dxftype() == "INSERT" and self._compute_block_has_arc(e.dxf.name, depth + 1):
                return True
        return False

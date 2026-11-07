"""DWG/DXF -> descriptive JSON (ARCHITECTURE.md, module 1).

Every model-space entity is listed with its raw geometry (in drawing units, untransformed) and a
classification (wall / window / door / room / room_label / furniture / annotation / hatch / other)
with the reason it was chosen. Layers and block definitions are summarized as well. This is the
inspection view of a drawing: what the 3D pipeline "sees" before geometry reconstruction.
"""

import io
import math
from collections import Counter, defaultdict
from typing import Any

from ezdxf import bbox, recover
from ezdxf.document import Drawing
from ezdxf.entities import DXFGraphic
from ezdxf.enums import InsertUnits
from ezdxf.lldxf.const import DXFError

from app.services.classify import CATEGORIES, DrawingClassifier, LayerPatterns

MAX_POINTS = 2000  # per polyline; keeps pathological drawings bounded


class DrawingReadError(Exception):
    pass


def read_dxf(data: bytes) -> Drawing:
    try:
        doc, _auditor = recover.read(io.BytesIO(data))
    except (DXFError, OSError, ValueError) as exc:
        raise DrawingReadError(f"Could not read DXF: {exc}") from exc
    return doc


def _r(value: float, digits: int = 4) -> float:
    return round(float(value), digits) + 0.0


def _pt(v) -> list[float]:
    return [_r(v[0]), _r(v[1])]


def _bbox(entity: DXFGraphic) -> dict | None:
    try:
        box = bbox.extents([entity], fast=True)
    except Exception:  # some proxy/invalid entities cannot be measured
        return None
    if not box.has_data:
        return None
    return {"min": _pt(box.extmin), "max": _pt(box.extmax)}


def describe_geometry(entity: DXFGraphic) -> dict[str, Any]:
    """Raw geometry of one entity in drawing units."""
    t = entity.dxftype()
    dxf = entity.dxf
    if t == "LINE":
        s, e = dxf.start, dxf.end
        return {"start": _pt(s), "end": _pt(e), "length": _r(math.hypot(e.x - s.x, e.y - s.y))}
    if t == "LWPOLYLINE":
        points = [_pt(p) for p in entity.vertices_in_wcs()][:MAX_POINTS]
        bulges = [_r(b) for *_xy, b in entity.get_points("xyb")][:MAX_POINTS]
        out: dict[str, Any] = {"points": points, "closed": bool(entity.closed)}
        if any(bulges):
            out["bulges"] = bulges
        return out
    if t == "POLYLINE":
        return {"points": [_pt(p) for p in entity.points_in_wcs()][:MAX_POINTS], "closed": bool(entity.is_closed)}
    if t == "CIRCLE":
        return {"center": _pt(dxf.center), "radius": _r(dxf.radius)}
    if t == "ARC":
        return {
            "center": _pt(dxf.center),
            "radius": _r(dxf.radius),
            "startAngle": _r(dxf.start_angle, 2),
            "endAngle": _r(dxf.end_angle, 2),
        }
    if t == "ELLIPSE":
        return {"center": _pt(dxf.center), "majorAxis": _pt(dxf.major_axis), "ratio": _r(dxf.ratio)}
    if t == "INSERT":
        out = {
            "block": dxf.name,
            "insert": _pt(dxf.insert),
            "rotation": _r(dxf.get("rotation", 0.0), 3),
            "scale": [_r(dxf.get("xscale", 1.0)), _r(dxf.get("yscale", 1.0))],
        }
        attribs = {a.dxf.tag: a.dxf.text for a in entity.attribs if a.dxf.get("text")}
        if attribs:
            out["attributes"] = attribs
        return out
    if t == "TEXT":
        return {"text": dxf.get("text", ""), "position": _pt(dxf.insert), "height": _r(dxf.get("height", 0))}
    if t == "MTEXT":
        return {"text": entity.plain_text(), "position": _pt(dxf.insert), "height": _r(dxf.get("char_height", 0))}
    if t == "HATCH":
        return {
            "pattern": dxf.get("pattern_name", ""),
            "solid": bool(dxf.get("solid_fill", 0)),
            "paths": len(entity.paths),
        }
    if t == "DIMENSION":
        try:
            return {"measurement": _r(entity.get_measurement())}
        except Exception:
            return {}
    return {}


def describe_drawing(data: bytes, patterns: LayerPatterns, *, include_entities: bool = True) -> dict[str, Any]:
    doc = read_dxf(data)
    msp = doc.modelspace()
    classifier = DrawingClassifier(doc, patterns)

    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    try:
        units_name = InsertUnits(insunits).name.lower()
    except ValueError:
        units_name = f"unknown({insunits})"

    entities: list[dict[str, Any]] = []
    by_category: Counter = Counter()
    layer_types: dict[str, Counter] = defaultdict(Counter)
    layer_categories: dict[str, Counter] = defaultdict(Counter)
    block_uses: dict[str, Counter] = defaultdict(Counter)
    furniture: Counter = Counter()

    for entity in msp:
        layer = entity.dxf.get("layer", "0")
        cls = classifier.classify(entity, layer)
        by_category[cls.category] += 1
        layer_types[layer][entity.dxftype()] += 1
        layer_categories[layer][cls.category] += 1
        if entity.dxftype() == "INSERT":
            block_uses[entity.dxf.name][cls.category] += 1
        if cls.category == "furniture":
            furniture[cls.furniture_category or "unknown"] += 1
        if include_entities:
            item: dict[str, Any] = {
                "handle": entity.dxf.handle,
                "type": entity.dxftype(),
                "layer": layer,
                "category": cls.category,
                "reason": cls.reason,
                "geometry": describe_geometry(entity),
            }
            if cls.furniture_category:
                item["furnitureCategory"] = cls.furniture_category
            box = _bbox(entity)
            if box:
                item["bbox"] = box
            entities.append(item)

    blocks = []
    for name, uses in sorted(block_uses.items(), key=lambda kv: -sum(kv[1].values())):
        block = doc.blocks.get(name)
        size = None
        if block is not None:
            try:
                box = bbox.extents(block, fast=True)
                if box.has_data:
                    size = [_r(box.size.x), _r(box.size.y)]
            except Exception:
                size = None
        category = uses.most_common(1)[0][0]
        entry: dict[str, Any] = {
            "name": name,
            "insertCount": sum(uses.values()),
            "category": category,
            "entityTypes": dict(Counter(e.dxftype() for e in block)) if block is not None else {},
            "size": size,
        }
        blocks.append(entry)

    try:
        ext = bbox.extents(msp, fast=True)
        extents = {"min": _pt(ext.extmin), "max": _pt(ext.extmax)} if ext.has_data else None
    except Exception:
        extents = None

    layers = [
        {
            "name": name,
            "entityCount": sum(types.values()),
            "types": dict(types.most_common()),
            "categories": dict(layer_categories[name].most_common()),
        }
        for name, types in sorted(layer_types.items(), key=lambda kv: -sum(kv[1].values()))
    ]

    result: dict[str, Any] = {
        "dxfVersion": doc.dxfversion,
        "units": {"insunits": insunits, "name": units_name, "measurement": int(doc.header.get("$MEASUREMENT", 0) or 0)},
        "extents": extents,
        "summary": {
            "entityCount": sum(by_category.values()),
            "byCategory": {c: by_category[c] for c in CATEGORIES if by_category[c]},
            "furnitureByCategory": dict(furniture.most_common()),
        },
        "layers": layers,
        "blocks": blocks,
    }
    if include_entities:
        result["entities"] = entities
    return result

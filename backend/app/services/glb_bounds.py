"""Measure a GLB model's axis-aligned bounding box (scene graph transforms applied)."""

import json
import math
import struct
from pathlib import Path

Matrix = list[list[float]]
_IDENTITY: Matrix = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def _mul(a: Matrix, b: Matrix) -> Matrix:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _node_matrix(node: dict) -> Matrix:
    if "matrix" in node:
        m = node["matrix"]  # column-major
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    tx, ty, tz = node.get("translation", (0, 0, 0))
    x, y, z, w = node.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = node.get("scale", (1, 1, 1))
    rot = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    t = (tx, ty, tz)
    return [[rot[i][0] * sx, rot[i][1] * sy, rot[i][2] * sz, t[i]] for i in range(3)] + [[0, 0, 0, 1]]


def glb_bounds(path: Path) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return (size, center) in glTF model units."""
    data = path.read_bytes()
    chunk_len, chunk_type = struct.unpack_from("<II", data, 12)
    if data[:4] != b"glTF" or chunk_type != 0x4E4F534A:
        raise ValueError(f"{path.name} is not a GLB file")
    gltf = json.loads(data[20 : 20 + chunk_len])
    lo, hi = [math.inf] * 3, [-math.inf] * 3

    def visit(index: int, parent: Matrix) -> None:
        node = gltf["nodes"][index]
        matrix = _mul(parent, _node_matrix(node))
        if "mesh" in node:
            for primitive in gltf["meshes"][node["mesh"]]["primitives"]:
                accessor = gltf["accessors"][primitive["attributes"]["POSITION"]]
                amin, amax = accessor["min"], accessor["max"]
                for cx in (amin[0], amax[0]):
                    for cy in (amin[1], amax[1]):
                        for cz in (amin[2], amax[2]):
                            for i in range(3):
                                v = matrix[i][0] * cx + matrix[i][1] * cy + matrix[i][2] * cz + matrix[i][3]
                                lo[i], hi[i] = min(lo[i], v), max(hi[i], v)
        for child in node.get("children", []):
            visit(child, matrix)

    for root in gltf["scenes"][gltf.get("scene", 0)]["nodes"]:
        visit(root, _IDENTITY)
    size = tuple(round(hi[i] - lo[i], 3) for i in range(3))
    center = tuple(round((hi[i] + lo[i]) / 2, 3) for i in range(3))
    return size, center  # type: ignore[return-value]

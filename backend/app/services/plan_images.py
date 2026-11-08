"""Pictures for the AI's visual review (ARCHITECTURE.md §4.5): the original drawing and the model built from
it, cropped to the building at the same scale, so a vision model can compare them room by room.

* drawing.png — the DWG/DXF as drawn (every layer, grey levels on white): furniture symbols, door swings, labels.
* model.png — what the parser understood: walls (grey), doors (orange) and windows (blue) with their IDs,
  furniture footprints with ID and category, room names.

Pure CPU work on bytes: runs in a worker process (workers.run) with the parse time limit.
"""

import io
import math

import matplotlib

matplotlib.use("Agg")

from ezdxf import bbox as dxf_bbox  # noqa: E402
from ezdxf.addons.drawing import Frontend, RenderContext  # noqa: E402
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration  # noqa: E402
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend  # noqa: E402
from matplotlib import pyplot as plt  # noqa: E402
from matplotlib.patches import Polygon  # noqa: E402

from app.schemas.building import Building, Point2  # noqa: E402
from app.services.drawing_reader import read_dxf  # noqa: E402
from app.services.guardrails import Box, furniture_box, wall_boxes  # noqa: E402

LONG_SIDE_PX = 1600  # big enough to read labels and IDs, small enough for a vision model
MARGIN = 0.6  # m around the building
DPI = 100


def _crop(building: Building) -> tuple[float, float, float, float] | None:
    points = [p for w in building.walls for p in (w.start, w.end)]
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs) - MARGIN, min(ys) - MARGIN, max(xs) + MARGIN, max(ys) + MARGIN


def _figure(width_m: float, height_m: float):
    scale = LONG_SIDE_PX / max(width_m, height_m)
    fig = plt.figure(figsize=(width_m * scale / DPI, height_m * scale / DPI), dpi=DPI)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    return fig, ax


def _png(fig) -> bytes:
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=DPI, facecolor="white")
    plt.close(fig)
    return out.getvalue()


def _corners(box: Box) -> list[Point2]:
    vx, vy = -box.uy, box.ux
    return [
        (box.cx + su * box.hu * box.ux + sv * box.hv * vx, box.cy + su * box.hu * box.uy + sv * box.hv * vy)
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1))
    ]


def render_drawing(dxf: bytes, crop: tuple[float, float, float, float], factor: float, origin: Point2) -> bytes:
    """The original drawing inside `crop` (plan meters), black on white."""
    x0, y0, x1, y1 = crop
    to_drawing = lambda x, y: ((x + origin[0]) / factor, (y + origin[1]) / factor)  # noqa: E731
    (dx0, dy0), (dx1, dy1) = to_drawing(x0, y0), to_drawing(x1, y1)
    doc = read_dxf(dxf)

    def inside(entity) -> bool:  # skip other sheets and details: faster, and nothing to confuse the model
        try:
            box = dxf_bbox.extents([entity], fast=True)
        except Exception:  # noqa: BLE001
            return True
        if not box.has_data:
            return False
        return box.extmax.x >= dx0 and box.extmin.x <= dx1 and box.extmax.y >= dy0 and box.extmin.y <= dy1

    fig, ax = _figure(x1 - x0, y1 - y0)
    # Grey levels on white: crisp lines, and room labels stay legible inside their filled boxes.
    config = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.MONOCHROME_LIGHT_BG)
    Frontend(RenderContext(doc), MatplotlibBackend(ax), config=config).draw_layout(
        doc.modelspace(), finalize=False, filter_func=inside
    )
    ax.set_xlim(dx0, dx1)
    ax.set_ylim(dy0, dy1)
    ax.set_aspect("equal", adjustable="datalim")
    return _png(fig)


def render_model(building: Building, crop: tuple[float, float, float, float]) -> bytes:
    """The parsed model inside `crop`, with the IDs the review answers with."""
    x0, y0, x1, y1 = crop
    fig, ax = _figure(x1 - x0, y1 - y0)
    small = max(5.0, min(9.0, 60.0 / max(x1 - x0, y1 - y0) * 4))
    for wall, box in wall_boxes(building.walls):
        ax.add_patch(Polygon(_corners(box), closed=True, facecolor="#9a9a9a", edgecolor="none"))
        for opening in wall.openings:
            d = ((wall.end[0] - wall.start[0]) / wall.length, (wall.end[1] - wall.start[1]) / wall.length)
            a = (wall.start[0] + d[0] * opening.offset, wall.start[1] + d[1] * opening.offset)
            b = (a[0] + d[0] * opening.width, a[1] + d[1] * opening.width)
            color = "#1f6fd1" if opening.type == "window" else "#e07b00"
            ax.plot([a[0], b[0]], [a[1], b[1]], color=color, linewidth=4, solid_capstyle="butt")
            label = {"facecolor": "white", "edgecolor": "none", "pad": 0.5, "alpha": 0.8}
            ax.text((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, opening.id, color=color, fontsize=small, ha="center",
                    va="center", bbox=label)  # fmt: skip
    for item in building.furniture:
        box = furniture_box(item)
        ax.add_patch(Polygon(_corners(box), closed=True, facecolor="#f3e2c7", edgecolor="#7a4f1d", linewidth=1))
        fx, fy = item.facing
        ax.annotate("", xy=(box.cx + fx * box.hv, box.cy + fy * box.hv), xytext=(box.cx, box.cy),
                    arrowprops={"arrowstyle": "->", "color": "#7a4f1d", "lw": 0.8})  # fmt: skip
        ax.text(box.cx, box.cy, f"{item.id.replace('furniture_', 'f')}\n{item.category}", fontsize=small * 0.9,
                ha="center", va="center", color="#3d2508")  # fmt: skip
    for room in building.rooms:
        if room.label_position:
            ax.text(*room.label_position, room.name, fontsize=small * 1.2, ha="center", va="center", color="#0a7d32",
                    fontweight="bold")  # fmt: skip
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal", adjustable="datalim")
    return _png(fig)


def render_pair(dxf: bytes, building: Building, factor: float, origin: Point2) -> tuple[bytes, bytes] | None:
    """(drawing.png, model.png), or None when there is nothing to show."""
    crop = _crop(building)
    if crop is None or not factor or not all(math.isfinite(v) for v in crop):
        return None
    return render_drawing(dxf, crop, factor, origin), render_model(building, crop)

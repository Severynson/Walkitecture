"""The GLB furniture library in frontend/public/furniture (served at /furniture/) and the rules for matching drawing blocks to it.

`native_size` / `native_center` were measured from each GLB (glTF axes: x = width, y = up, z = depth),
because the models are authored at inconsistent scales; the scene spec therefore always carries
an explicit per-axis scale computed from the drawing's footprint. `real_size` (meters, w x h x d)
is the typical real-world size used when the drawing gives no usable footprint.
`front` is the side the model's front (seat, doors, drawers, open side) faces once loaded by Babylon:
"+z" (the usual glTF convention) unless measured otherwise; `real_size` is (width across the front,
height, depth along the front).
"""

import re
from dataclasses import dataclass

FURNITURE_URL_PREFIX = "/furniture/"


@dataclass(frozen=True)
class FurnitureModel:
    file: str
    category: str
    native_size: tuple[float, float, float]
    native_center: tuple[float, float, float]
    real_size: tuple[float, float, float]
    variant: str = "default"
    front: str = "+z"  # +z | -z | +x | -x (Babylon world, as loaded)

    @property
    def url(self) -> str:
        return FURNITURE_URL_PREFIX + self.file


CATALOG: tuple[FurnitureModel, ...] = (
    FurnitureModel("bed_king_by_quaternius.glb", "bed", (3.064, 1.215, 3.953), (0.0, 0.607, 0.0), (1.93, 0.6, 2.03), "double"),
    FurnitureModel("bed_single_by_quaternius.glb", "bed", (1.979, 1.355, 3.94), (0.023, 0.677, 0.0), (0.99, 0.6, 1.9), "single"),
    FurnitureModel("chair_by_quaternius.glb", "chair", (0.762, 1.768, 0.825), (0.0, 0.881, -0.032), (0.5, 0.9, 0.55)),
    FurnitureModel("office_chair_quaternius.glb", "chair", (0.722, 1.128, 0.807), (-0.0, 0.564, -0.038), (0.6, 1.0, 0.65), "office"),
    FurnitureModel("closet_by_quaternius.glb", "closet", (1.553, 2.978, 0.87), (-0.001, 1.481, 0.005), (1.2, 2.1, 0.6)),
    FurnitureModel("couch_medium_by_quaternius.glb", "sofa", (4.688, 1.91, 2.203), (0.0, 0.942, -0.082), (2.0, 0.85, 0.9)),
    FurnitureModel("couch_small_quarternius.glb", "sofa", (2.885, 1.91, 2.203), (-0.0, 0.942, -0.082), (0.9, 0.85, 0.85), "armchair"),
    FurnitureModel("lounge_sofa_corner_by_kenney.glb", "sofa", (0.98, 0.46, 0.98), (-0.49, 0.23, 0.49), (2.4, 0.85, 2.4), "corner"),
    FurnitureModel("desk_by_creativetrio.glb", "desk", (1.25, 0.433, 0.622), (0.32, 0.216, 0.006), (1.4, 0.75, 0.7)),
    FurnitureModel("table_by_darwin_yamamoto.glb", "table", (2.135, 1.104, 0.821), (-0.001, -0.12, 0.001), (1.6, 0.75, 0.9), "rectangular"),
    FurnitureModel("table_large_circular_by_creativetrio.glb", "table", (1.269, 0.452, 1.269), (0.0, 0.226, 0.0), (1.2, 0.75, 1.2), "round"),
    FurnitureModel("toilet_by_quaternius.glb", "toilet", (1.197, 1.901, 1.691), (0.0, 0.937, -0.054), (0.4, 0.8, 0.7)),
    FurnitureModel("bathtub_by_kenney.glb", "bathtub", (1.19, 0.42, 0.56), (-0.595, 0.21, 0.28), (1.7, 0.55, 0.75)),
    FurnitureModel("shower_round_by_kenney.glb", "shower", (0.562, 1.094, 0.562), (-0.281, 0.547, 0.281), (0.9, 1.9, 0.9), front="-z"),
    FurnitureModel("kitchen_sink_by_kay_lousberg.glb", "sink", (3.0, 1.947, 2.0), (0.0, 0.973, 0.0), (1.2, 0.9, 0.7)),
    FurnitureModel("kitchen_cabinet_by_kenney.glb", "kitchen", (0.43, 0.45, 0.45), (-0.215, 0.225, 0.225), (0.6, 0.9, 0.6)),
    FurnitureModel("kitchen_bar_by_kenney.glb", "kitchen", (0.43, 0.42, 0.21), (-0.215, 0.21, 0.105), (0.9, 0.9, 0.45), "bar"),
    FurnitureModel("kitchen_fridge_quarternius.glb", "fridge", (1.298, 3.308, 1.302), (0.0, 1.661, 0.067), (0.7, 1.8, 0.7)),
    FurnitureModel("cabinet_by_creativetrio.glb", "cabinet", (0.496, 0.492, 0.298), (-0.0, 0.246, -0.136), (0.9, 0.85, 0.5)),
    FurnitureModel("file_cabinet_j-toastie.glb", "cabinet", (0.76, 1.546, 0.659), (0.092, 0.756, -0.0), (0.45, 1.3, 0.6), "file", front="+x"),
    FurnitureModel("drawer_by_quaternius.glb", "dresser", (2.794, 1.332, 1.153), (0.0, 0.677, 0.021), (1.2, 0.8, 0.5)),
    FurnitureModel("night_stand_quaternius.glb", "nightstand", (0.952, 0.945, 0.952), (0.0, 0.476, -0.0), (0.5, 0.55, 0.45)),
    FurnitureModel("medium_book_shelf_zsky.glb", "bookshelf", (2.782, 2.362, 0.44), (-0.42, 1.067, -0.062), (1.2, 1.0, 0.35)),
)  # fmt: skip

# The door leaf hung in single doors (services/doors.py): not furniture. Width runs along its z axis, hinge
# edge at -z, knob at +z; 1.92 m tall; the 0.22 m depth includes the knobs.
DOOR_LEAF = FurnitureModel(
    "door_by_wesley_thompson.glb", "door", (0.219, 1.92, 0.93), (-0.002, -0.136, -0.223), (0.9, 2.1, 0.045)
)

# Keyword patterns (matched against block names, then layer names) -> category. First match wins, so
# specific names come before generic ones ("bedside table" is a nightstand, "kitchen cabinet" a kitchen unit).
CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (category, re.compile(pattern, re.IGNORECASE))
    for category, pattern in (
        # English + Spanish vocabulary (the example drawings in /examples are Spanish).
        ("toilet", r"toilet|\bwc|w\.c|water.?closet|lavatory|commode|inodoro|retrete"),
        ("bathtub", r"bath.?tub|ba[ñn]era|\btub\b|jacuzzi|hidromasaje"),
        ("shower", r"shower|ducha"),
        ("sink", r"\bsink|fregadero|fregadera|lavaplatos|lavavajilla"),
        ("fridge", r"fridge|refrigerat|nevera|frigor|heladera"),
        ("nightstand", r"night.?stand|night.?table|bedside|mesita.?de.?noche|mesilla|velador"),
        ("bed", r"\bbed|bed\b|bett|king|queen|twin|bunk|cama|litera"),
        ("sofa", r"sofa|sofá|couch|settee|lounge|loveseat|divan|armchair|sill[oó]n|butaca"),
        ("desk", r"desk|escritorio|work.?station|wkst|\bws\b|cubicle|bench.?desk|office.?table|bureau"),
        ("chair", r"chair|seat|stool|\bchr\b|silla|sill-|asiento|taburete"),
        ("kitchen", r"kitchen|cocina|counter|encimera|\bbar\b|barra"),
        ("dresser", r"dresser|drawers?\b|chest|c[oó]moda|cajonera|chiffonier"),
        ("bookshelf", r"book|shelf|shelving|estanter|librer"),
        ("cabinet", r"cabinet|sideboard|credenza|aparador|archiv|filing|file"),
        ("table", r"table|\btbl\b|dining|conference|meeting|coffee|mesa|mesita|comedor|reuni[oó]n"),
        ("closet", r"closet|wardrobe|armoire|locker|armario|ropero|comodin"),
    )
)
OFFICE_CHAIR = re.compile(r"office|oficina|swivel|girator|operativ|task|desk", re.IGNORECASE)
FILE_CABINET = re.compile(r"file|filing|archiv", re.IGNORECASE)
ARMCHAIR_MAX = 1.3  # m: a "sofa" no longer than this is an armchair
KITCHEN_BAR_DEPTH = 0.45  # m: shallower kitchen units are bar counters

DOOR_PATTERN = re.compile(r"door|\bdr\b|tür|porte|puerta", re.IGNORECASE)
WINDOW_PATTERN = re.compile(r"window|\bwin\b|wndw|glaz|fenster|fenetre|ventana", re.IGNORECASE)


def category_for(*names: str | None) -> str | None:
    """Furniture category for a block (or layer) name, or None. Earlier names take precedence."""
    for name in names:
        if not name:
            continue
        for category, pattern in CATEGORY_PATTERNS:
            if pattern.search(name):
                return category
    return None


SIZE_GUESS_CATEGORIES = ("chair", "desk", "table", "closet")  # generic shapes; beds/sofas/toilets get names
# Fixtures (sinks, showers, fixed/built-in furniture) must not be size-guessed into chairs or desks.
NO_SIZE_GUESS_LAYERS = re.compile(
    r"sanit|plumb|ba[ñn]o|bath|cocina|kitchen|electr|equip|fijo|fixed|fixt", re.IGNORECASE
)
SIZE_GUESS_TOLERANCE = 0.3


def category_from_size(width: float, depth: float, layer: str = "") -> str | None:
    """Guess a generic category from a footprint (m), or None if nothing fits within 30%."""
    if NO_SIZE_GUESS_LAYERS.search(layer):
        return None
    short, long_ = sorted((width, depth))
    best: tuple[float, str] | None = None
    for model in CATALOG:
        if model.category not in SIZE_GUESS_CATEGORIES:
            continue
        ref_short, ref_long = sorted((model.real_size[0], model.real_size[2]))
        error = max(abs(short - ref_short) / ref_short, abs(long_ - ref_long) / ref_long)
        if error <= SIZE_GUESS_TOLERANCE and (best is None or error < best[0]):
            best = (error, model.category)
    return best[1] if best else None


def choose_model(
    category: str, width: float, depth: float, round_shape: bool = False, name: str = ""
) -> FurnitureModel | None:
    """Pick the library model that best fits a category, footprint (meters), and block name (hints such as
    "office" or "file")."""
    candidates = [m for m in CATALOG if m.category == category]
    if not candidates:
        return None
    if category == "bed":
        return _variant(candidates, "double" if min(width, depth) >= 1.3 else "single")
    if category == "table":
        squareish = abs(width - depth) <= 0.15 * max(width, depth, 1e-6)
        return _variant(candidates, "round" if round_shape and squareish else "rectangular")
    if category == "sofa":
        corner = min(width, depth) > 1.4 and abs(width - depth) < 0.35 * max(width, depth)
        armchair = max(width, depth) <= ARMCHAIR_MAX
        return _variant(candidates, "corner" if corner else "armchair" if armchair else "default")
    if category == "chair":
        return _variant(candidates, "office" if OFFICE_CHAIR.search(name) else "default")
    if category == "kitchen":
        return _variant(candidates, "bar" if min(width, depth) < KITCHEN_BAR_DEPTH else "default")
    if category == "cabinet":
        return _variant(candidates, "file" if FILE_CABINET.search(name) else "default")
    return candidates[0]


def _variant(models: list[FurnitureModel], variant: str) -> FurnitureModel:
    return next((m for m in models if m.variant == variant), models[0])

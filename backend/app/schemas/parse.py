from typing import Literal

from pydantic import Field

from app.schemas.base import CamelModel

UnitName = Literal["auto", "mm", "cm", "m", "in", "ft"]
FurnitureCategory = Literal["bed", "chair", "sofa", "desk", "table", "closet", "toilet"]
BlockRole = Literal["door", "window", "bed", "chair", "sofa", "desk", "table", "closet", "toilet", "ignore"]
FURNITURE_CATEGORIES: tuple[str, ...] = ("bed", "chair", "sofa", "desk", "table", "closet", "toilet")


class ParseOptions(CamelModel):
    """Per-request overrides for plan parsing. Omitted fields fall back to server settings."""

    units: UnitName = Field(
        default="auto",
        description="Drawing units. 'auto' reads $INSUNITS from the file and guesses from extents if missing.",
    )
    scale: float | None = Field(
        default=None, gt=0, description="Explicit drawing-units -> meters multiplier (calibration). Overrides units."
    )
    wall_style: Literal["auto", "centerline", "double_line"] = Field(
        default="auto",
        description="'centerline': each wall-layer line is a wall centerline. "
        "'double_line': walls are drawn as parallel face lines (2 or more per wall); thickness is inferred. "
        "'auto': double_line when most wall lines have a parallel partner, else centerline.",
    )
    wall_thickness: float | None = Field(default=None, gt=0, le=1.0, description="Centerline-mode wall thickness (m).")
    min_wall_thickness: float = Field(
        default=0.05, gt=0, le=1.0, description="Double-line mode: smallest face gap (m)."
    )
    max_wall_thickness: float = Field(default=0.5, gt=0, le=1.5, description="Double-line mode: largest face gap (m).")
    ceiling_height: float | None = Field(default=None, ge=2.1, le=6.0, description="Wall height (default 10 ft).")
    place_furniture: bool = Field(default=True, description="Match furniture blocks to the GLB library.")
    window_height: float | None = Field(default=None, gt=0)
    window_sill_height: float | None = Field(default=None, ge=0)
    door_height: float | None = Field(default=None, gt=0)
    wall_layers: list[str] | None = Field(default=None, description="Layer-name globs, e.g. ['WALLS', 'A-WALL*'].")
    window_layers: list[str] | None = None
    door_layers: list[str] | None = None
    opening_layers: list[str] | None = Field(
        default=None, description="Mixed door/window layers: blocks with a swing arc become doors, others windows."
    )
    room_layers: list[str] | None = None
    furniture_layers: list[str] | None = None
    block_overrides: dict[str, BlockRole] | None = Field(
        default=None,
        description="Force a category for named blocks (case-insensitive), e.g. {'18140101': 'chair', 'PC40': 'door'}. "
        "'ignore' drops the block entirely. Takes precedence over every other rule.",
    )

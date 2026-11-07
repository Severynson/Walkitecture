from typing import Literal

from pydantic import Field

from app.schemas.base import CamelModel

UnitName = Literal["auto", "mm", "cm", "m", "in", "ft"]


class ParseOptions(CamelModel):
    """Per-request overrides for plan parsing. Omitted fields fall back to server settings."""

    units: UnitName = Field(
        default="auto",
        description="Drawing units. 'auto' reads $INSUNITS from the file and guesses from extents if missing.",
    )
    scale: float | None = Field(
        default=None, gt=0, description="Explicit drawing-units -> meters multiplier (calibration). Overrides units."
    )
    wall_style: Literal["centerline", "double_line"] = Field(
        default="centerline",
        description="'centerline': each wall-layer line is a wall centerline. "
        "'double_line': walls are drawn as pairs of parallel face lines; thickness is inferred.",
    )
    wall_thickness: float | None = Field(default=None, gt=0, le=1.0, description="Centerline-mode wall thickness (m).")
    min_wall_thickness: float = Field(
        default=0.05, gt=0, le=1.0, description="Double-line mode: smallest face gap (m)."
    )
    max_wall_thickness: float = Field(default=0.6, gt=0, le=1.5, description="Double-line mode: largest face gap (m).")
    ceiling_height: float | None = Field(default=None, ge=2.1, le=6.0)
    window_height: float | None = Field(default=None, gt=0)
    window_sill_height: float | None = Field(default=None, ge=0)
    door_height: float | None = Field(default=None, gt=0)
    wall_layers: list[str] | None = Field(default=None, description="Layer-name globs, e.g. ['WALLS', 'A-WALL*'].")
    window_layers: list[str] | None = None
    door_layers: list[str] | None = None
    room_layers: list[str] | None = None

"""Canonical building model: the single source of truth that the renderer turns into meshes.

All coordinates are 2D plan coordinates in meters (x, y). The renderer maps plan `y` to its
horizontal depth axis; heights are separate scalar fields.
"""

import math
from typing import Literal

from pydantic import Field

from app.schemas.base import CamelModel

Point2 = tuple[float, float]


class Opening(CamelModel):
    id: str
    type: Literal["window", "door"]
    offset: float = Field(
        ge=0, description="Distance (m) along the wall from its start point to the opening's left edge."
    )
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    sill_height: float = Field(default=0.0, ge=0)

    @property
    def center(self) -> float:
        return self.offset + self.width / 2

    @property
    def top(self) -> float:
        return self.sill_height + self.height


class Wall(CamelModel):
    id: str
    start: Point2
    end: Point2
    thickness: float = Field(gt=0)
    height: float = Field(gt=0)
    openings: list[Opening] = Field(default_factory=list)

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)


class Room(CamelModel):
    id: str
    name: str
    polygon: list[Point2] | None = None
    label_position: Point2 | None = None


class Footprint(CamelModel):
    position: Point2
    width: float
    depth: float


class FurnitureItem(CamelModel):
    """A piece of furniture matched to a GLB model from the frontend furniture library."""

    id: str
    category: str = Field(description="A library category (schemas/parse.py FURNITURE_CATEGORIES): bed, chair, ...")
    model: str = Field(description="GLB file name in frontend/public/furniture")
    source_block: str | None = Field(default=None, description="DWG block (INSERT) this came from")
    matched_by: Literal["block_name", "layer", "size", "llm", "manual"] = "block_name"
    position: Point2 = Field(description="Footprint center in plan coordinates (m)")
    facing: Point2 = Field(description="Unit vector the model's front points to (away from the nearest wall)")
    width: float = Field(gt=0, description="Footprint extent across the facing direction (m)")
    depth: float = Field(gt=0, description="Footprint extent along the facing direction (m)")
    height: float = Field(gt=0)
    drawn: Footprint | None = Field(
        default=None,
        description="The block's footprint as drawn in the plan, kept when the guardrails moved or resized the "
        "item (position/width/depth then hold the corrected footprint)",
    )


class Building(CamelModel):
    schema_version: Literal[1] = 1
    units: Literal["meters"] = "meters"
    ceiling_height: float = Field(gt=0)
    walls: list[Wall] = Field(default_factory=list)
    rooms: list[Room] = Field(default_factory=list)
    furniture: list[FurnitureItem] = Field(default_factory=list)

    def find_wall(self, wall_id: str) -> Wall | None:
        return next((w for w in self.walls if w.id == wall_id), None)

    def find_opening(self, opening_id: str) -> tuple[Wall, Opening] | None:
        for wall in self.walls:
            for opening in wall.openings:
                if opening.id == opening_id:
                    return wall, opening
        return None

    def kind_of(self, object_id: str) -> str | None:
        """Return 'wall', 'window', 'door', 'room', or None for an unknown ID."""
        if self.find_wall(object_id):
            return "wall"
        if found := self.find_opening(object_id):
            return found[1].type
        if any(r.id == object_id for r in self.rooms):
            return "room"
        if any(f.id == object_id for f in self.furniture):
            return "furniture"
        return None

"""Render-ready Babylon.js scene spec (ARCHITECTURE.md module 2: "JSON into Babylon.js").

Coordinates: Babylon world space, meters, y up. Plan x -> Babylon x, plan y -> Babylon z.
Boxes: `MeshBuilder.CreateBox(id, {width: size[0], height: size[1], depth: size[2]})`, then
`mesh.position = position` (the box center) and `mesh.rotation.y = rotationY`.
"""

from typing import Literal

from pydantic import Field

from app.schemas.base import CamelModel

Vec3 = tuple[float, float, float]


class BoxMesh(CamelModel):
    id: str
    kind: Literal["wall", "header", "sill", "glass", "floor", "ceiling", "ground", "boundary", "sky"] = Field(
        description="ground / boundary / sky: the open ground around the building, the backdrop walls at the map's "
        "end, and the sky canopy resting on them (first-person scenery, not part of the building)"
    )
    wall_id: str | None = None
    opening_id: str | None = None
    position: Vec3 = Field(description="Box center")
    size: Vec3 = Field(description="[width (local x, along the wall), height, depth (local z, wall thickness)]")
    rotation_y: float = Field(description="Radians; aligns local +x with the wall direction")


class DoorLeafSpec(CamelModel):
    """A hinged door leaf (single doors). The frontend puts a pivot node at `hinge` with `rotationY` (closed)
    and adds `swing` to open it. In pivot space, x runs along the closed leaf from the hinge to the latch, y up."""

    url: str = Field(description="GLB of the leaf")
    hinge: Vec3 = Field(description="Hinge axis at floor level, on the wall's centerline")
    rotation_y: float = Field(description="Pivot rotation when closed (radians)")
    swing: float = Field(description="Radians added to rotationY to open the door (sign = which side it opens to)")
    model_position: Vec3 = Field(description="Model root position in pivot space")
    model_rotation_y: float = Field(description="Model root rotation in pivot space")
    scaling: Vec3 = Field(description="Model root scaling")
    size: Vec3 = Field(description="Leaf [width, height, thickness] (the collider, from the hinge along pivot x)")


class OpeningSpec(CamelModel):
    id: str
    type: Literal["window", "door"]
    wall_id: str
    position: Vec3 = Field(description="Center of the opening")
    width: float
    height: float
    sill_height: float
    rotation_y: float
    leaf: DoorLeafSpec | None = Field(default=None, description="Single doors: the interactive leaf")


class FurnitureSpec(CamelModel):
    id: str
    category: str
    model: str
    url: str = Field(description="Path of the GLB in the frontend furniture library")
    position: Vec3 = Field(description="Position of a parent TransformNode holding the imported GLB")
    rotation_y: float = Field(description="Radians, so the model's front (glTF +z) points along `facing`")
    scaling: Vec3 = Field(description="Per-axis scale fitting the drawing's footprint and a realistic height")
    footprint_center: Vec3 = Field(description="Where the scaled model's footprint center ends up (on the floor)")
    footprint: tuple[float, float] = Field(description="[width, depth] in meters")
    facing: tuple[float, float] = Field(description="Front direction in Babylon x/z")


class RoomLabel(CamelModel):
    id: str
    name: str
    position: Vec3


class MaterialSpec(CamelModel):
    color: str
    alpha: float = 1.0
    texture: str | None = Field(
        default=None,
        description="Image URL: the landscape backdrop (boundary walls, horizon at eye level) or the sky (canopy)",
    )


class CameraSpec(CamelModel):
    position: Vec3 = Field(description="First-person spawn point at eye height")
    target: Vec3
    eye_height: float = Field(description="Walker's eye level above the floor (m)")
    body_height: float = Field(default=1.8288, description="Walker's height (m) for collisions: 6 ft by default")


class Bounds(CamelModel):
    min: Vec3
    max: Vec3


class BabylonScene(CamelModel):
    schema_version: Literal[1] = 1
    units: Literal["meters"] = "meters"
    coordinate_system: str = "Babylon world space, y up; plan x -> x, plan y -> z"
    wall_height: float
    bounds: Bounds = Field(description="The building (walls)")
    map_bounds: Bounds = Field(description="The walkable map: the building plus open ground, ringed by boundary walls")
    meshes: list[BoxMesh]
    openings: list[OpeningSpec]
    furniture: list[FurnitureSpec]
    rooms: list[RoomLabel]
    camera: CameraSpec
    materials: dict[str, MaterialSpec]

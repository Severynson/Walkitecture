"""Allowlisted edit commands. These are data, never code: each maps to one known function."""

from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from app.schemas.base import CamelModel
from app.schemas.parse import FurnitureCategory


class ResizeWindowParams(CamelModel):
    factor: float | None = Field(default=None, gt=0, description="Width multiplier, e.g. 1.5 = 50% wider.")
    width: float | None = Field(default=None, gt=0, description="Absolute new width in meters.")
    height: float | None = Field(default=None, gt=0, description="Absolute new height in meters.")
    sill_height: float | None = Field(default=None, ge=0, description="Absolute new sill height in meters.")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.factor is not None and self.width is not None:
            raise ValueError("Provide either 'factor' or 'width', not both.")
        if all(v is None for v in (self.factor, self.width, self.height, self.sill_height)):
            raise ValueError("Provide at least one of 'factor', 'width', 'height', 'sillHeight'.")
        return self


class FactorParams(CamelModel):
    factor: float = Field(gt=0, description="Width multiplier applied to every window.")


class CeilingParams(CamelModel):
    height: float = Field(gt=0, description="New ceiling height in meters.")


class MoveWallParams(CamelModel):
    offset: float = Field(
        description=(
            "Signed distance in meters along the wall's left-hand normal "
            "(perpendicular, to the left when looking from start to end)."
        )
    )


class ResizeSelectedWindow(CamelModel):
    operation: Literal["resize_selected_window"]
    target_id: str
    parameters: ResizeWindowParams


class ResizeWindows(CamelModel):
    operation: Literal["resize_windows"]
    target_id: None = None
    parameters: FactorParams


class SetCeilingHeight(CamelModel):
    operation: Literal["set_ceiling_height"]
    target_id: None = None
    parameters: CeilingParams


class MoveSelectedWall(CamelModel):
    operation: Literal["move_selected_wall"]
    target_id: str
    parameters: MoveWallParams


EditCommand = Annotated[
    ResizeSelectedWindow | ResizeWindows | SetCeilingHeight | MoveSelectedWall,
    Field(discriminator="operation"),
]
EditCommandAdapter: TypeAdapter[EditCommand] = TypeAdapter(EditCommand)

ALLOWED_OPERATIONS = ("resize_selected_window", "resize_windows", "set_ceiling_height", "move_selected_wall")


# --- Review-only operations (ARCHITECTURE.md §4.3) ------------------------------------------------
# Proposed by the LLM reviewer and executed by the same edit engine (validation, undo, history), but not
# accepted by POST /edits: the UI allowlist above stays small.


class NoParams(CamelModel):
    pass


class MergeWallsParams(CamelModel):
    wall_ids: list[str] = Field(min_length=2, max_length=20)


class OpeningTypeParams(CamelModel):
    type: Literal["door", "window"]


class FurnitureCategoryParams(CamelModel):
    category: FurnitureCategory


class RoomNameParams(CamelModel):
    name: str = Field(min_length=1, max_length=80)


class AddOpeningParams(CamelModel):
    type: Literal["door", "window"]
    offset: float = Field(ge=0, description="Distance (m) along the wall from its start to the opening's left edge.")
    width: float = Field(gt=0)


class DeleteWall(CamelModel):
    operation: Literal["delete_wall"]
    target_id: str
    parameters: NoParams = Field(default_factory=NoParams)


class MergeWalls(CamelModel):
    operation: Literal["merge_walls"]
    target_id: None = None
    parameters: MergeWallsParams


class SetOpeningType(CamelModel):
    operation: Literal["set_opening_type"]
    target_id: str
    parameters: OpeningTypeParams


class DeleteOpening(CamelModel):
    operation: Literal["delete_opening"]
    target_id: str
    parameters: NoParams = Field(default_factory=NoParams)


class SetFurnitureCategory(CamelModel):
    operation: Literal["set_furniture_category"]
    target_id: str
    parameters: FurnitureCategoryParams


class DeleteFurniture(CamelModel):
    operation: Literal["delete_furniture"]
    target_id: str
    parameters: NoParams = Field(default_factory=NoParams)


class RenameRoom(CamelModel):
    operation: Literal["rename_room"]
    target_id: str
    parameters: RoomNameParams


class AddOpening(CamelModel):
    """Guardrails only (a door into a room that has no entrance); never proposed free-form by the LLM."""

    operation: Literal["add_opening"]
    target_id: str
    parameters: AddOpeningParams


ReviewCommand = Annotated[
    DeleteWall | MergeWalls | SetOpeningType | DeleteOpening | SetFurnitureCategory | DeleteFurniture | RenameRoom,
    Field(discriminator="operation"),
]
ReviewCommandAdapter: TypeAdapter[ReviewCommand] = TypeAdapter(ReviewCommand)
REVIEW_OPERATIONS = (
    "delete_wall", "merge_walls", "set_opening_type", "delete_opening", "set_furniture_category",
    "delete_furniture", "rename_room",
)  # fmt: skip

AnyCommand = Annotated[
    ResizeSelectedWindow | ResizeWindows | SetCeilingHeight | MoveSelectedWall | DeleteWall | MergeWalls
    | SetOpeningType | DeleteOpening | SetFurnitureCategory | DeleteFurniture | RenameRoom | AddOpening,
    Field(discriminator="operation"),
]  # fmt: skip
AnyCommandAdapter: TypeAdapter[AnyCommand] = TypeAdapter(AnyCommand)

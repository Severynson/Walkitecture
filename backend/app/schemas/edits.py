"""Allowlisted edit commands. These are data, never code: each maps to one known function."""

from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from app.schemas.base import CamelModel


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

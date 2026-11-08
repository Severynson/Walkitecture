"""Request / response bodies for the HTTP API (camelCase on the wire)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.schemas.babylon import BabylonScene
from app.schemas.base import CamelModel
from app.schemas.building import Building, Point2
from app.schemas.checks import CheckReport
from app.schemas.edits import EditCommand
from app.services.geometry import Problem
from app.services.llm_classify import ClassificationProposal


class ErrorBody(CamelModel):
    code: str
    message: str
    problems: list[Problem] = Field(default_factory=list)


class ErrorResponse(CamelModel):
    error: ErrorBody


class Credentials(CamelModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=8, max_length=128)


class UserOut(CamelModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    created_at: datetime


class TokenResponse(CamelModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut


class PlanSummary(CamelModel):
    id: uuid.UUID
    filename: str
    source_format: Literal["dwg", "dxf"]
    file_size: int
    status: Literal["uploaded", "processing", "parsed", "failed"]
    stage: str | None = Field(
        default=None, description="While processing: the pipeline step (reading, classifying, checking, reviewing...)"
    )
    model_version: int
    has_model: bool
    error: str | None
    created_at: datetime
    updated_at: datetime


class PlanDetail(PlanSummary):
    parse_warnings: list[str]
    parse_stats: dict[str, Any] | None
    report: CheckReport | None = Field(default=None, description="What the automatic checks found and changed")


class SceneResponse(CamelModel):
    plan_id: uuid.UUID
    version: int
    model: Building = Field(description="Canonical building JSON (source of truth for edits)")
    babylon: BabylonScene = Field(description="Render-ready Babylon.js scene generated from the model")


class ParseResponse(CamelModel):
    plan: PlanDetail
    scene: SceneResponse


class EditRequest(CamelModel):
    command: EditCommand
    base_version: int | None = Field(
        default=None, description="Model version the client edited against; mismatches return 409."
    )
    source: Literal["api", "ui", "openai", "rules"] = "api"


class EditResponse(CamelModel):
    plan_id: uuid.UUID
    applied: bool
    version: int
    affected_ids: list[str]
    summary: str
    model: Building
    babylon: BabylonScene


class EditHistoryItem(CamelModel):
    id: int
    version: int
    command: dict[str, Any]
    affected_ids: list[str]
    summary: str
    source: str
    created_at: datetime


class ReplaceSceneRequest(CamelModel):
    model: Building
    base_version: int | None = None


class InterpretRequest(CamelModel):
    text: str = Field(min_length=1, max_length=500)
    selected_id: str | None = None
    plan_id: uuid.UUID | None = Field(default=None, description="Use the stored model of this plan as context.")
    model: Building | None = Field(default=None, description="Inline model context (when no planId is given).")
    camera_forward: Point2 | None = Field(
        default=None, description="Camera forward direction in plan coordinates; resolves 'forward'/'back'."
    )
    apply: bool = Field(default=False, description="Also apply the command to planId if it validates.")
    base_version: int | None = None


class EditPreview(CamelModel):
    affected_ids: list[str]
    summary: str


class InterpretResponse(CamelModel):
    command: EditCommand | None
    source: Literal["openai", "rules"]
    explanation: str
    clarification: str | None = None
    notes: list[str] = Field(default_factory=list)
    valid: bool | None = Field(default=None, description="Whether the command passes validation (null if unknown).")
    problems: list[Problem] = Field(default_factory=list)
    preview: EditPreview | None = None
    applied: EditResponse | None = None


class ClassifyRequest(CamelModel):
    min_confidence: float = Field(default=0.5, ge=0, le=1, description="Drop LLM proposals below this confidence")


class ClassifyResponse(CamelModel):
    proposal: ClassificationProposal
    applied: bool
    result: ParseResponse | None = Field(default=None, description="The re-parsed plan (apply=true only)")


class HealthResponse(CamelModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "unavailable"]
    dwg_converter: bool
    interpreter: Literal["openai", "rules"]

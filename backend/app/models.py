"""PostgreSQL tables. Raw uploads live in bytea columns (deferred, so listings never load them)."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True)  # stored lower-case
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    source_format: Mapped[str] = mapped_column(String(8))  # dwg | dxf
    file_size: Mapped[int] = mapped_column(Integer)
    original_file: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    dxf_file: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    status: Mapped[str] = mapped_column(String(16), default="uploaded")  # uploaded | processing | parsed | failed
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)  # pipeline step while processing
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model_version: Mapped[int] = mapped_column(Integer, default=0)
    # Descriptive JSON of every drawing entity + its classification (ARCHITECTURE.md module 1).
    drawing: Mapped[dict | None] = mapped_column(JSONB, deferred=True)
    # Render-ready Babylon.js scene spec generated from `model` (ARCHITECTURE.md module 2).
    scene: Mapped[dict | None] = mapped_column(JSONB, deferred=True)
    parse_warnings: Mapped[list] = mapped_column(JSONB, default=list)
    parse_stats: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Layer/block roles the model was built with (the AI's classification when it was used).
    parse_options: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # What the automatic checks found, decided and changed (schemas/checks.py).
    report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    edits: Mapped[list["PlanEdit"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", passive_deletes=True, order_by="PlanEdit.id"
    )


class PlanEdit(Base):
    """One applied edit. `previous_model` is the snapshot that Undo restores."""

    __tablename__ = "plan_edits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)  # model_version after this edit
    command: Mapped[dict] = mapped_column(JSONB)
    previous_model: Mapped[dict] = mapped_column(JSONB, deferred=True)
    affected_ids: Mapped[list] = mapped_column(JSONB)
    summary: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="api")  # api | openai | rules | ui
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    plan: Mapped[Plan] = relationship(back_populates="edits")

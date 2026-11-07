"""Idempotent development seed: a login account that owns ready-to-walk sample plans.

Runs on every container start (after migrations) and via `python scripts/seed.py`.
* The account is created once; an existing account is never modified (its password is kept).
* Each sample plan is created once per account (matched by filename) and fully processed:
  the built-in demo apartment plus every .dwg/.dxf in the examples directory.
A sample that fails (e.g. DWG without `dwg2dxf`) is reported and skipped, never fatal.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Plan, User
from app.schemas.api import Credentials
from app.schemas.parse import ParseOptions
from app.services import plan_store
from app.services.auth import hash_password
from app.services.dwg_convert import ConversionError, converter_available, detect_format
from app.services.dxf_parser import ParseError
from app.services.sample_plan import DEMO_FILENAME, build_demo_dxf

logger = logging.getLogger(__name__)

REPO_EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


@dataclass
class SeedReport:
    user: str | None = None
    user_created: bool = False
    plans_created: list[str] = field(default_factory=list)
    plans_existing: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "<file>: <reason>"

    def summary(self) -> str:
        if self.user is None:
            return "Seed skipped: SEED_USERNAME / SEED_PASSWORD not set."
        parts = [f"user {self.user!r} {'created' if self.user_created else 'already exists'}"]
        if self.plans_created:
            parts.append(f"created plans {self.plans_created}")
        if self.plans_existing:
            parts.append(f"kept {len(self.plans_existing)} existing plans")
        if self.skipped:
            parts.append(f"skipped {self.skipped}")
        return "Seed: " + "; ".join(parts) + "."


def sample_files(settings: Settings) -> list[tuple[str, bytes]]:
    """(filename, bytes) of every sample plan to seed."""
    samples = [(DEMO_FILENAME, build_demo_dxf())]
    directory = Path(settings.seed_examples_dir) if settings.seed_examples_dir else REPO_EXAMPLES
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() in (".dwg", ".dxf") and path.is_file():
                samples.append((path.name, path.read_bytes()))
    return samples


async def run_seed(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> SeedReport:
    report = SeedReport()
    if not (settings.seed_username and settings.seed_password):
        return report
    try:
        creds = Credentials(username=settings.seed_username, password=settings.seed_password)
    except ValidationError as exc:
        raise ValueError(f"Invalid SEED_USERNAME / SEED_PASSWORD: {exc.errors()[0]['msg']}") from exc
    report.user = creds.username.lower()

    async with sessions() as session:
        user, report.user_created = await _ensure_user(session, report.user, creds.password)
        user_id = user.id
        if not settings.seed_sample_plans:
            return report
        existing = set((await session.execute(select(Plan.filename).where(Plan.owner_id == user_id))).scalars())

    for filename, data in sample_files(settings):
        if filename in existing:
            report.plans_existing.append(filename)
            continue
        fmt = detect_format(filename, data)
        if fmt is None:
            report.skipped.append(f"{filename}: not a DWG/DXF file")
            continue
        if fmt == "dwg" and not converter_available(settings):
            report.skipped.append(f"{filename}: dwg2dxf not installed")
            continue
        async with sessions() as session:
            try:
                await _create_plan(session, user_id, filename, fmt, data, settings)
                report.plans_created.append(filename)
            except (ConversionError, ParseError) as exc:
                report.skipped.append(f"{filename}: {exc}")
    return report


async def _ensure_user(session: AsyncSession, username: str, password: str) -> tuple[User, bool]:
    user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is not None:
        return user, False
    user = User(username=username, password_hash=hash_password(password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:  # another replica seeded concurrently
        await session.rollback()
        user = (await session.execute(select(User).where(User.username == username))).scalar_one()
        return user, False
    return user, True


async def _create_plan(session: AsyncSession, user_id, filename: str, fmt: str, data: bytes, settings: Settings):
    plan = Plan(
        owner_id=user_id, filename=filename, source_format=fmt, file_size=len(data), original_file=data,
        parse_warnings=[],
    )  # fmt: skip
    session.add(plan)
    await session.commit()
    plan = await plan_store.get_plan(session, plan.id, user_id, for_update=True, files=True)
    try:
        await plan_store.parse_plan(session, plan, ParseOptions(), settings)
    except (ConversionError, ParseError):
        # Keep a single, failed record instead of retrying a broken sample on every start.
        if plan.status != "failed":
            await session.delete(plan)
            await session.commit()
        raise

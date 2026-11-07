"""Seed the database with a login account that owns the sample plans (idempotent).

    SEED_USERNAME=demo SEED_PASSWORD=walkitecture-demo uv run python scripts/seed.py
    docker compose exec backend python scripts/seed.py

Runs automatically on container start (see the Dockerfile CMD). Safe to run repeatedly.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.seed import run_seed  # noqa: E402


async def main() -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        report = await run_seed(async_sessionmaker(engine, expire_on_commit=False), settings)
    except ValueError as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(report.summary())
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(main()))

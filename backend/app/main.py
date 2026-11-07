import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.api import ai, auth, edits, health, interpret, plans
from app.api.errors import register_error_handlers
from app.config import Settings, get_settings
from app.db import engine
from app.spa import mount_spa, spa_available

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    problems = settings.production_problems()
    if problems:  # fail fast: uvicorn cannot import the app, the container restarts and logs why
        raise RuntimeError("Refusing to start with APP_ENV=production: " + " ".join(problems))

    app = FastAPI(
        title="Walkitecture API",
        version="0.1.0",
        description="Floor-plan ingestion (DWG/DXF), canonical building model, validated edits, "
        "and natural-language edit interpretation.",
        lifespan=lifespan,
    )
    if settings.cors_origins:  # same-origin in production (SPA served by the backend), so CORS is dev-only
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    register_error_handlers(app)
    for module in (health, auth, plans, edits, interpret, ai):
        app.include_router(module.router)

    # Production image: serve the built SPA (registered last so API and /docs routes win).
    if static_dir := spa_available(settings.static_dir):
        mount_spa(app, static_dir)
    else:

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse("/docs")

    return app


app = create_app()

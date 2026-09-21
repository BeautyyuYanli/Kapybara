"""Mountable control routers; the application owns their resource lifecycle."""

from pathlib import Path

from fastapi import APIRouter
from pydantic_ai import Agent

from kapy.control.models import ModelService
from kapy.control.sessions import SessionService

from .models import create_model_router
from .sessions import create_session_router

__all__ = [
    "create_router",
    "create_model_router",
    "create_session_router",
    "create_frontend_router",
]


def create_router[DepsT, OutputT](
    models: ModelService,
    sessions: SessionService,
    *,
    agent: Agent[DepsT, OutputT] | None = None,
    deps: DepsT = None,
    realtime_output: bool = True,
    output_flush_interval: float = 0.5,
) -> APIRouter:
    """Compose HTTP and WebSocket routes under /api using the borrowed services."""
    router = APIRouter(prefix="/api")
    router.include_router(create_model_router(models))
    router.include_router(
        create_session_router(
            sessions,
            agent=agent,
            deps=deps,
            realtime_output=realtime_output,
            output_flush_interval=output_flush_interval,
        )
    )
    return router


def create_frontend_router(dist_dir: Path) -> APIRouter:
    """Mount an explicit build directory, failing setup if the entry is missing.

    Include beside the host's API router. Native frontend routing handles SPA
    fallback while retaining 404 responses for missing static resources.
    """
    if not (dist_dir / "index.html").is_file():
        raise RuntimeError(f"Frontend entry point is missing in {dist_dir}")
    router = APIRouter()
    router.frontend("/app", directory=dist_dir, fallback="index.html", check_dir=True)
    return router

"""Mountable tmpv2 control routers; the application owns resources and authentication."""

from fastapi import APIRouter
from pydantic_ai import Agent

from kapy.tmpv2.control.models import ModelService
from kapy.tmpv2.control.sessions import SessionService

from .models import create_model_router
from .sessions import create_session_router

__all__ = ["create_router", "create_model_router", "create_session_router"]


def create_router[DepsT, OutputT](
    models: ModelService,
    sessions: SessionService,
    *,
    agent: Agent[DepsT, OutputT],
    deps: DepsT = None,
    realtime_output: bool = True,
    output_flush_interval: float = 0.5,
) -> APIRouter:
    """Compose /api routes; host dependencies must authorize both HTTP and WebSocket."""
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

"""The HTTP process owns its resources through lifespan; Uvicorn drains requests first."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from kapy.application.resources import open_resources
from kapy.application.sessions import create_session_service
from kapy.control.models import ModelService

from . import create_frontend_router, create_router
from .settings import HttpSettings


class _InflightRequests:
    """Track full ASGI calls, including BackgroundTasks, until their cleanup completes."""

    def __init__(self, app: ASGIApp, active: set[asyncio.Task]) -> None:
        self.app, self.active = app, active

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        try:
            await self.app(scope, receive, send)
        finally:
            self.active.discard(task)


def create_app(settings: HttpSettings) -> FastAPI:
    """Serve HTTP, WebSocket and the optional frontend without access authentication."""
    active: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with open_resources(settings.common) as resources:
            sessions = create_session_service(resources, settings.common)
            router = create_router(
                ModelService(resources.core_session_factory),
                sessions,
                realtime_output=settings.common.realtime_output,
                output_flush_interval=settings.common.output_flush_interval,
            )
            app.include_router(router)
            if settings.frontend_dist is not None:
                app.include_router(create_frontend_router(settings.frontend_dist))
            try:
                yield
            finally:
                # Uvicorn may cancel timed-out requests without joining their cleanup.
                for task in active:
                    task.cancel()
                await asyncio.gather(*active, return_exceptions=True)
                # Remove lifespan-installed routes if a test/application re-enters lifespan.
                del app.router.routes[initial_route_count:]
                app.openapi_schema = None

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(_InflightRequests, active=active)
    initial_route_count = len(app.router.routes)
    return app

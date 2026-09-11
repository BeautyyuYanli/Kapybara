"""The HTTP process owns its resources through lifespan; Uvicorn drains requests first."""

import asyncio
import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, WebSocketException
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from kapy.tmpv2.agent_output import AgentOutputService
from kapy.tmpv2.application.agent import create_agent
from kapy.tmpv2.application.resources import open_resources
from kapy.tmpv2.control.models import ModelService
from kapy.tmpv2.control.sessions import SessionService

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
    """Protect HTTP and WebSocket with the existing operator bearer-token convention."""

    async def authorize(connection: HTTPConnection) -> None:
        scheme, _, token = connection.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token, settings.control_token.get_secret_value()
        ):
            if connection.scope["type"] == "websocket":
                raise WebSocketException(code=1008)
            raise HTTPException(status_code=401, detail="Bearer authentication required")

    active: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with open_resources(settings.common) as resources:
            sessions = SessionService(
                resources.core_session_factory,
                output_service=AgentOutputService(
                    resources.valkey,
                    channel_prefix=settings.common.valkey_namespace + ":agent-output",
                ),
                heartbeat_interval=settings.common.heartbeat_interval,
                heartbeat_timeout=settings.common.heartbeat_timeout,
            )
            router = create_router(
                ModelService(resources.core_session_factory),
                sessions,
                agent=create_agent(),
                realtime_output=settings.common.realtime_output,
                output_flush_interval=settings.common.output_flush_interval,
            )
            app.include_router(router, dependencies=[Depends(authorize)])
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

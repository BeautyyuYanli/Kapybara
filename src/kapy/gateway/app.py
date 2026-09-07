"""FastAPI composition with explicit resource and background-task ownership."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import httpx2
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from psycopg_pool import AsyncConnectionPool

from kapy.agent import ModelBackend, OpenAICompatibleBackend, ScriptTool, apply_patch_plugin
from kapy.rpc import JsonParams, JsonValue, RpcError, RpcPeer, dispatch_json
from kapy.settings import Settings, load_settings

from .auth import Authenticator, bearer
from .control import ControlService
from .frontends import FrontendContext, FrontendFactory
from .machines import Connection, MachineRegistry
from .storage import Metadata, migrate

MAX_FRAME = 1_048_576


async def supervise(name: str, run: Callable[[], Awaitable[None]]) -> None:
    logger = logging.getLogger(__name__)
    failures = 0
    while True:
        try:
            await run()
        except Exception as exc:
            logger.error("%s exited unexpectedly (%s); restarting", name, type(exc).__name__)
            await asyncio.sleep(min(30, 2 ** min(failures, 5)))
            failures += 1
        else:
            logger.info("%s stopped", name)
            return


def create_app(
    settings: Settings | None = None,
    *,
    model_backend: ModelBackend | None = None,
    frontend_factories: Mapping[str, FrontendFactory] | None = None,
    plugins: Sequence[ScriptTool] | None = None,
) -> FastAPI:
    config = settings if settings is not None else load_settings()
    from .telegram import TelegramFrontend

    factories: dict[str, FrontendFactory] = {"telegram": TelegramFrontend}
    if frontend_factories is not None:
        factories.update(frontend_factories)
    enabled = config.enabled_frontends()
    unknown = set(enabled) - factories.keys()
    if unknown:
        raise ValueError(f"Unknown frontends: {', '.join(sorted(unknown))}")
    if "telegram" in enabled and (not config.telegram_bot_token or config.telegram_chat_id is None):
        raise ValueError("Telegram requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    selected_plugins = plugins
    if selected_plugins is None:
        if set(config.tool_plugins) - {"apply_patch"} or len(config.tool_plugins) != len(
            set(config.tool_plugins)
        ):
            raise ValueError("Unknown or repeated tool plugin name")
        selected_plugins = tuple(apply_patch_plugin() for _ in config.tool_plugins)
    auth = Authenticator(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from kapy.agent import AgentPayloadStore, Runner, RunnerConfig
        from kapy.skills import SkillService
        from kapy.state import SessionService
        from kapy.state import migrate as migrate_state

        config.require_control(model_backend_supplied=model_backend is not None)
        database_url = config.database_url.get_secret_value()
        await migrate_state(database_url, schema=config.database_schema)
        await migrate(database_url, schema=config.database_schema)
        async with AsyncExitStack() as resources:
            pool = AsyncConnectionPool(database_url, open=False, min_size=1, max_size=8)
            await resources.enter_async_context(pool)
            await pool.wait()
            backend = model_backend
            if backend is None:
                http = await resources.enter_async_context(httpx2.AsyncClient(trust_env=False))
                assert config.model_api_key is not None
                backend = OpenAICompatibleBackend(
                    base_url=config.model_base_url, api_key=config.model_api_key, http_client=http
                )
            metadata = Metadata(pool, schema=config.database_schema)
            skills = SkillService(pool, schema=config.database_schema)
            payloads = AgentPayloadStore(pool, schema=config.database_schema)
            await skills.initialize()
            await payloads.initialize()
            machines = MachineRegistry(auth, metadata, lambda: sessions)
            resources.push_async_callback(machines.aclose)

            async def authorize_wait(session_id, waiting_ids) -> None:
                await control.authorize_wait(session_id, waiting_ids)

            assert config.context_window_tokens is not None
            runner = Runner(
                RunnerConfig(
                    model=config.model,
                    context_window_tokens=config.context_window_tokens,
                    max_output_tokens=config.max_output_tokens,
                    compression_ratio=config.compression_ratio,
                    keep_recent_ratio=config.keep_recent_ratio,
                    media_max_bytes=config.media_max_bytes,
                ),
                machines,
                model_backend=backend,
                payload_store=payloads,
                authorize_wait=authorize_wait,
                plugins=selected_plugins,
            )

            async def run(context):
                return await control.run(context)

            sessions = SessionService(
                database_url=database_url,
                valkey_url=config.valkey_url.get_secret_value(),
                schema=config.database_schema,
                namespace=config.valkey_namespace,
                runner=run,
            )
            control = ControlService(
                settings=config,
                metadata=metadata,
                sessions=sessions,
                skills=skills,
                runner=runner,
                machines=machines,
                payload_store=payloads,
            )
            # State may resume a runner on entry; the closure already points to ControlService.
            await resources.enter_async_context(sessions)
            app.state.control = control
            app.state.machines = machines
            app.state.metadata = metadata
            context = FrontendContext(config, control, pool, config.database_schema)
            tasks = [
                asyncio.create_task(
                    supervise("gateway-recovery", control.background),
                    name="gateway-recovery",
                )
            ]
            tasks.extend(
                asyncio.create_task(
                    supervise(type(frontend).__name__, frontend.run),
                )
                for frontend in (factories[name](context) for name in enabled)
            )
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # State exits next, while its Runner can still access peers, HTTP and metadata.

    app = FastAPI(lifespan=lifespan)

    @app.post("/rpc")
    async def rpc(request: Request) -> Response:
        try:
            principal = auth.operator(bearer(request.headers.get("authorization")))
        except RpcError:
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        body = bytearray()
        async for block in request.stream():
            if len(body) + len(block) > MAX_FRAME:
                return Response(status_code=413)
            body.extend(block)
        try:
            payload = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            payload = ""  # Parse-error through the shared codec.

        async def handler(method: str, params: JsonParams) -> JsonValue:
            if not isinstance(params, dict):
                raise RpcError(-32602, "Business parameters must be an object")
            return await app.state.control.call(method, params, principal=principal)

        result = await dispatch_json(payload, handler)
        return (
            Response(status_code=204)
            if result is None
            else Response(
                result,
                media_type="application/json",
            )
        )

    @app.websocket("/rpc/machines/{machine_id}")
    async def machine(websocket: WebSocket, machine_id: str) -> None:
        try:
            auth.machine(machine_id, bearer(websocket.headers.get("authorization")))
        except RpcError:
            await websocket.close(code=1008)
            return
        protocols = websocket.scope.get("subprotocols", ())
        if "kapy.jsonrpc.v1" not in protocols:
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol="kapy.jsonrpc.v1")

        async def receive() -> str | None:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                return None
            if message["type"] == "websocket.disconnect":
                return None
            text = message.get("text")
            if text is None or len(text.encode("utf-8")) > MAX_FRAME:
                await websocket.close(code=1009 if text is not None else 1003)
                return None
            return text

        async def close() -> None:
            try:
                await websocket.close()
            except RuntimeError, OSError:
                pass

        async def handler(method: str, params: JsonParams) -> JsonValue:
            return await app.state.machines.proxy(connection, method, params, app.state.control)

        peer = RpcPeer(
            send_text=websocket.send_text,
            receive_text=receive,
            close_transport=close,
            handler=handler,
        )
        connection = Connection(machine_id, peer)
        async with peer:
            try:
                await app.state.machines.register(connection)
                await peer.wait_closed()
            finally:
                await app.state.machines.unregister(connection)

    return app

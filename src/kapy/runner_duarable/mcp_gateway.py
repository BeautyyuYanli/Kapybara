"""A heartbeating Activity owns one stdio Gateway for the whole Agent run.

SDK tool Activities can execute on different Workers. This owner exposes a
temporary authenticated HTTP relay to the same entered stdio client, so those
Activities share the process without passing live clients across Temporal.
The Workflow cancels the owner in finally; Worker shutdown closes its contexts.
A retried owner creates a fresh Gateway and signals its replacement endpoint.
MCP process memory is ephemeral and is not restored after an owner failure.
"""

import asyncio
import os
import secrets
import socket
from collections.abc import AsyncIterator, Generator, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.providers.proxy import ProxyProvider
from fastmcp.utilities.components import FastMCPComponent
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError

from kapy.layers.mcps import Config, make_toolset
from kapy.layers.mcps.models import GatewayEndpoint

READY_SIGNAL = "kapy.mcp_gateway_ready"


class RelayServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None]:
        # Multiple run relays share this process. The Worker owns signal handling.
        yield


class RunProxyProvider(ProxyProvider):
    async def get_tasks(self) -> Sequence[FastMCPComponent]:
        # Gateway proxies do not support tasks. Startup must not enumerate the
        # backend before actual discovery, especially for slow stdio upstreams.
        return []


@asynccontextmanager
async def relay(server: FastMCP, token: str) -> AsyncIterator[GatewayEndpoint]:
    """Bind a private run relay on an ephemeral port; drain it before client close.

    Workers must be able to reach the owner's advertised host. By default this
    is its hostname's IPv4 address; KAPY_MCP_RUN_HOST overrides the advertised host.
    The port is not published through Kapy's public HTTP interface.
    """
    advertised = os.environ.get("KAPY_MCP_RUN_HOST") or socket.gethostbyname(socket.gethostname())
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        if ":" in advertised:
            advertised = f"[{advertised}]"
        endpoint = GatewayEndpoint(
            url=f"http://{advertised}:{port}/mcp",
            token=token,
            attempt=activity.info().attempt,
        )
        http = RelayServer(
            uvicorn.Config(
                server.http_app(path="/mcp"),
                log_level="warning",
                access_log=False,
                timeout_graceful_shutdown=2,
            )
        )
        serving = asyncio.create_task(http.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(30):
                while not http.started:
                    if serving.done():
                        await serving
                        raise RuntimeError("MCP run relay did not start")
                    await asyncio.sleep(0.01)
            yield endpoint
        finally:
            http.should_exit = True
            try:
                async with asyncio.timeout(5):
                    await asyncio.shield(serving)
            except TimeoutError:
                serving.cancel()
                with suppress(asyncio.CancelledError):
                    await serving


@activity.defn(name="kapy.hold_mcp_gateway")
async def hold_mcp_gateway(config: Config) -> None:
    """Hold the pipe until cancellation; announce readiness only after initialization.

    Only a newly generated per-run bearer reaches relay clients. It is never
    forwarded through the private pipe or injected into an upstream connection.
    Periodic status checks also reclaim the owner after Workflow termination.
    """
    info = activity.info()
    if info.workflow_id is None or info.workflow_run_id is None:
        raise ValueError("The MCP owner requires a Workflow execution")
    handle = activity.client().get_workflow_handle(info.workflow_id, run_id=info.workflow_run_id)

    async def beat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(1)

    beating = asyncio.create_task(beat())
    try:
        token = secrets.token_urlsafe(32)
        async with make_toolset(config) as tools:
            proxy = FastMCP(
                "MCP run relay",
                auth=StaticTokenVerifier(
                    tokens={token: {"client_id": info.workflow_run_id, "scopes": []}}
                ),
                providers=[RunProxyProvider(lambda: tools.mcp.client, cache_ttl=0)],
                mask_error_details=True,
            )
            proxy.provider_error_strategy = "raise"
            async with relay(proxy, token) as endpoint:
                await handle.signal(READY_SIGNAL, endpoint)
                while True:
                    await asyncio.sleep(10)
                    if (await handle.describe()).status != WorkflowExecutionStatus.RUNNING:
                        return
    except asyncio.CancelledError:
        details = activity.cancellation_details()
        if activity.is_worker_shutdown() and (details is None or not details.cancel_requested):
            # Worker retirement must retry the owner; Workflow cancellation must
            # acknowledge closure instead of creating another Gateway.
            raise ApplicationError(
                "MCP owner Worker stopped", type="MCPGatewayOwnerWorkerShutdown"
            ) from None
        raise
    finally:
        beating.cancel()
        with suppress(asyncio.CancelledError):
            await beating

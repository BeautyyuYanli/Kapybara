"""The uvx entry point and domain arguments; Execution owns local framing."""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx2
import typer

from kapy.execution import DaemonConfig, ProxyAuth, call_local_proxy, resolve_paths
from kapy.rpc import JsonObject, JsonValue, RpcDisconnected, RpcError, RpcTimeout
from kapy.settings import Settings, load_settings

app = typer.Typer(no_args_is_help=True)
control = typer.Typer(no_args_is_help=True)
session = typer.Typer(no_args_is_help=True)
history = typer.Typer(no_args_is_help=True)
skill = typer.Typer(no_args_is_help=True)
event = typer.Typer(no_args_is_help=True)
provider = typer.Typer(no_args_is_help=True)
provider_model = typer.Typer(no_args_is_help=True)
app.add_typer(control, name="control")
control.add_typer(session, name="session")
control.add_typer(history, name="history")
control.add_typer(skill, name="skill")
control.add_typer(event, name="event")
control.add_typer(provider, name="provider")
provider.add_typer(provider_model, name="model")


@dataclass
class Client:
    settings: Settings
    session_id: str | None
    machine_id: str | None

    def target(self, value: str | None = None) -> str:
        target = value or self.session_id
        if target is None:
            raise typer.BadParameter("Provide --session or KAPY_SESSION_ID")
        try:
            return str(UUID(target))
        except ValueError:
            raise typer.BadParameter("session must be a UUID") from None

    async def call(self, method: str, params: JsonObject, *, timeout: float = 60) -> JsonValue:  # noqa: ASYNC109
        settings = self.settings
        if settings.session_id or settings.session_token:
            if not settings.session_id or not settings.session_token:
                raise typer.BadParameter("Session context requires both ID and capability token")
            auth: ProxyAuth = {
                "kind": "session",
                "session_id": settings.session_id,
                "token": settings.session_token.get_secret_value(),
            }
        elif settings.control_token:
            auth = {"kind": "user", "token": settings.control_token.get_secret_value()}
        else:
            raise typer.BadParameter(
                "A session capability or explicit KAPY_CONTROL_TOKEN is required"
            )
        paths = resolve_paths(
            state_dir=settings.execution_state_dir,
            data_dir=settings.execution_data_dir,
            runtime_dir=settings.execution_runtime_dir,
        )
        if "request_id" in params and method != "session.wait":
            typer.echo(f"Request ID: {params['request_id']}", err=True)
            if method.startswith("skill.") and "archive_path" in params:
                typer.echo(f"Archive: {params['archive_path']}", err=True)
            sys.stderr.flush()
        return await call_local_proxy(
            settings.daemon_socket or paths.socket_path,
            method,
            params,
            auth=auth,
            timeout=timeout,
        )


def client(ctx: typer.Context) -> Client:
    return cast(Client, ctx.obj)


def display(result: JsonValue) -> None:
    typer.echo(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def invoke(ctx: typer.Context, method: str, params: JsonObject) -> None:
    display(asyncio.run(client(ctx).call(method, params)))


def input_text(
    value: str | None,
    source: Path | None,
    stdin: bool,
    *,
    required: bool = True,
) -> str | None:
    if sum((value is not None, source is not None, stdin)) > 1:
        raise typer.BadParameter("Provide text, --file or --stdin exclusively")
    if source is not None:
        return source.read_text(encoding="utf-8")
    if stdin or value == "-":
        return sys.stdin.read()
    if value is None and required:
        raise typer.BadParameter("Provide text, --file or --stdin")
    return value


def rid(request_id: UUID | None) -> str:
    return str(request_id or uuid4())


@control.callback()
def configure_control(
    ctx: typer.Context,
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    machine: Annotated[str | None, typer.Option("--machine")] = None,
) -> None:
    settings = load_settings()
    ctx.obj = Client(settings, session_id or settings.session_id, machine or settings.machine_id)


@app.command("server")
def server() -> None:
    """Run the execution daemon in the project machine container."""
    from kapy.execution import run_daemon

    settings = load_settings()
    if not settings.machine_id or not settings.machine_token:
        raise typer.BadParameter("KAPY_MACHINE_ID and KAPY_MACHINE_TOKEN are required")
    parsed = urlsplit(settings.control_url)
    gateway = urlunsplit(
        (
            {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parsed.scheme, ""),
            parsed.netloc,
            parsed.path.rstrip("/") + "/rpc/machines/" + quote(settings.machine_id, safe=""),
            "",
            "",
        )
    )
    config = DaemonConfig(
        machine_id=settings.machine_id,
        gateway_url=gateway,
        machine_token=settings.machine_token,
        state_dir=settings.execution_state_dir,
        data_dir=settings.execution_data_dir,
        runtime_dir=settings.execution_runtime_dir,
        child_env=settings.child_env,
        idle_disconnect_after_s=settings.idle_disconnect_after_s,
        idle_reconnect_after_s=settings.idle_reconnect_after_s,
    )
    asyncio.run(run_daemon(config))


@app.command("control-server")
def control_server(
    host: Annotated[str, typer.Option()] = "0.0.0.0",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
) -> None:
    """Run the control plane and configured frontends."""
    import uvicorn

    from kapy.gateway import create_app

    settings = load_settings()
    settings.require_control()
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        loop="uvloop",
        ws_max_size=1_048_576,
        ws_max_queue=4,
        ws_per_message_deflate=False,
    )


@control.command("call")
def raw_call(ctx: typer.Context, method: str, params: str = "{}") -> None:
    """Call a control method with a JSON object."""
    try:
        value = json.loads(params)
    except ValueError:
        raise typer.BadParameter("params must be valid JSON") from None
    if not isinstance(value, dict):
        raise typer.BadParameter("params must be a JSON object")
    invoke(ctx, method, value)


@session.command("create")
def create_session(
    ctx: typer.Context,
    text: Annotated[str | None, typer.Argument()] = None,
    file: Annotated[Path | None, typer.Option("--file")] = None,
    stdin: Annotated[bool, typer.Option("--stdin")] = False,
    title: Annotated[str, typer.Option()] = "",
    machine: Annotated[list[str] | None, typer.Option("--machine")] = None,
    default_machine: Annotated[str | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option(help="Registered model UUID")] = None,
    config_file: Annotated[Path | None, typer.Option()] = None,
    instructions: Annotated[str, typer.Option()] = "",
    request_id: Annotated[UUID | None, typer.Option()] = None,
    waiting_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    text = input_text(text, file, stdin, required=False)
    machines = machine or ([client(ctx).machine_id] if client(ctx).machine_id else [])
    config: JsonObject = read_json_file(config_file) if config_file else {}
    if instructions:
        config["instructions"] = instructions
    if model:
        config["model"] = {**cast(dict, config.get("model", {})), "model_id": model}
    invoke(
        ctx,
        "session.create",
        {
            "request_id": rid(request_id),
            "title": title,
            "machine_ids": cast(list[JsonValue], machines),
            "default_machine_id": default_machine or (machines[0] if len(machines) == 1 else None),
            "config": config,
            "input": text,
            "waiting_id": str(waiting_id) if waiting_id else None,
        },
    )


@session.command("get")
def get_session(
    ctx: typer.Context, session_id: Annotated[str | None, typer.Argument()] = None
) -> None:
    invoke(ctx, "session.get", {"session_id": client(ctx).target(session_id)})


@session.command("list")
def list_sessions(
    ctx: typer.Context,
    after: str | None = None,
    limit: Annotated[int, typer.Option(min=1, max=200)] = 100,
) -> None:
    invoke(ctx, "session.list", {"after": after, "limit": limit})


@session.command("delete")
def delete_session(
    ctx: typer.Context,
    session_id: Annotated[str | None, typer.Argument()] = None,
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    invoke(
        ctx,
        "session.delete",
        {"session_id": client(ctx).target(session_id), "request_id": rid(request_id)},
    )


@session.command("update")
def update_session(
    ctx: typer.Context,
    title: Annotated[str, typer.Option()],
    machine: Annotated[list[str], typer.Option("--machine")],
    default_machine: Annotated[str | None, typer.Option()] = None,
    config: Annotated[str, typer.Option()] = "{}",
    config_file: Annotated[Path | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option(help="Registered model UUID")] = None,
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    """Replace the session title, machines and configuration completely.

    State permits updates only while the session is waiting. Supply a registered
    model ID through --model, --config or --config-file; updates do not inherit
    the existing model selection. Other omitted configuration fields are cleared.
    Omitting --default-machine clears the prior default machine.
    """
    configuration = read_json_file(config_file) if config_file else json.loads(config)
    if model:
        configuration["model"] = {**cast(dict, configuration.get("model", {})), "model_id": model}
    invoke(
        ctx,
        "session.update",
        {
            "session_id": client(ctx).target(),
            "request_id": rid(request_id),
            "title": title,
            "machine_ids": cast(list[JsonValue], machine),
            "default_machine_id": default_machine,
            "config": configuration,
        },
    )


@session.command("input")
def input_session(
    ctx: typer.Context,
    text: Annotated[str | None, typer.Argument()] = None,
    file: Annotated[Path | None, typer.Option("--file")] = None,
    stdin: Annotated[bool, typer.Option("--stdin")] = False,
    steer: Annotated[bool, typer.Option()] = False,
    request_id: Annotated[UUID | None, typer.Option()] = None,
    waiting_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    text = input_text(text, file, stdin)
    invoke(
        ctx,
        "session.input",
        {
            "session_id": client(ctx).target(),
            "payload": text,
            "mode": "steer" if steer else "queue",
            "request_id": rid(request_id),
            "waiting_id": str(waiting_id) if waiting_id else None,
        },
    )


@session.command("output")
def output_session(
    ctx: typer.Context,
    after: Annotated[str | None, typer.Option()] = None,
    follow: Annotated[bool, typer.Option()] = False,
) -> None:
    async def read() -> None:
        cursor = after
        while True:
            result = cast(
                dict[str, Any],
                await client(ctx).call(
                    "session.output",
                    {
                        "session_id": client(ctx).target(),
                        "after": cursor,
                        "wait_seconds": 30 if follow else 0,
                    },
                ),
            )
            for record in result["items"]:
                display(record)
            cursor = result["next_cursor"]
            if not follow and not result["has_more"]:
                return

    asyncio.run(read())


@session.command("wait")
def wait_session(
    ctx: typer.Context,
    receipt: Annotated[UUID | None, typer.Argument()] = None,
    request_id: Annotated[UUID | None, typer.Option("--request-id")] = None,
    timeout: Annotated[float, typer.Option(min=0)] = 60,
) -> None:
    if (receipt is None) == (request_id is None):
        raise typer.BadParameter("Provide a request UUID or --request-id, exclusively")
    request_id = request_id or receipt

    async def wait() -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0, deadline - time.monotonic())
            result = cast(
                dict[str, Any],
                await client(ctx).call(
                    "session.wait",
                    {
                        "session_id": client(ctx).target(),
                        "request_id": str(request_id),
                        "wait_seconds": min(30, remaining),
                    },
                ),
            )
            if result["completion"] is not None or time.monotonic() >= deadline:
                display(result)
                return

    asyncio.run(wait())


@history.command("read")
def read_history(ctx: typer.Context, after: str | None = None, limit: int = 200) -> None:
    invoke(
        ctx, "history.read", {"session_id": client(ctx).target(), "after": after, "limit": limit}
    )


@history.command("search")
def search_history(ctx: typer.Context, query: str, substring: bool = False) -> None:
    invoke(
        ctx,
        "history.search",
        {
            "session_id": client(ctx).target(),
            "query": query,
            "mode": "substring" if substring else "fulltext",
        },
    )


@history.command("query")
def query_history(
    ctx: typer.Context,
    sql: Annotated[str | None, typer.Argument()] = None,
    file: Annotated[Path | None, typer.Option("--file")] = None,
    stdin: Annotated[bool, typer.Option("--stdin")] = False,
    params: str = "{}",
) -> None:
    sql = input_text(sql, file, stdin)
    invoke(
        ctx,
        "history.query",
        {"session_id": client(ctx).target(), "sql": sql, "params": json.loads(params)},
    )


@history.command("export")
def export_history(
    ctx: typer.Context, output: Annotated[Path | None, typer.Option()] = None
) -> None:
    async def export() -> None:
        after = snapshot = None
        destination = output.open("w", encoding="utf-8") if output else sys.stdout
        try:
            while True:
                page = cast(
                    dict[str, Any],
                    await client(ctx).call(
                        "history.export",
                        {
                            "session_id": client(ctx).target(),
                            "after": after,
                            "snapshot": snapshot,
                        },
                    ),
                )
                for record in page["items"]:
                    destination.write(json.dumps(record, ensure_ascii=False) + "\n")
                destination.flush()
                after, snapshot = page["next_cursor"], page["snapshot_cursor"]
                if not page["has_more"]:
                    break
        finally:
            if output:
                destination.close()

    asyncio.run(export())


@event.command("publish")
def publish(
    ctx: typer.Context, waiting_id: UUID, payload: str, request_id: UUID | None = None
) -> None:
    invoke(
        ctx,
        "event.publish",
        {"waiting_id": str(waiting_id), "request_id": rid(request_id), "payload": payload},
    )


@skill.command("list")
def list_skills(
    ctx: typer.Context, query: str | None = None, after_id: str | None = None, limit: int = 100
) -> None:
    invoke(ctx, "skill.list", {"query": query, "after_id": after_id, "limit": limit})


@skill.command("get")
def get_skill(ctx: typer.Context, skill_id: str) -> None:
    invoke(ctx, "skill.get", {"skill_id": skill_id})


@skill.command("read")
def read_skill(ctx: typer.Context, skill_id: str) -> None:
    invoke(ctx, "skill.read", {"skill_id": skill_id})


def transfer_params(ctx: typer.Context, archive: Path, request_id: UUID) -> JsonObject:
    current = client(ctx)
    if not current.machine_id:
        raise typer.BadParameter("Provide --machine or KAPY_MACHINE_ID for local archive transfer")
    return {
        "session_id": current.target(),
        "machine_id": current.machine_id,
        "archive_path": str(archive.absolute()),
        "request_id": str(request_id),
    }


@skill.command("upload")
def upload_skill(
    ctx: typer.Context,
    source: Annotated[Path | None, typer.Argument()] = None,
    archive: Annotated[Path | None, typer.Option()] = None,
    skill_id: Annotated[str | None, typer.Option()] = None,
    expected_revision: Annotated[int | None, typer.Option(min=1)] = None,
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    from kapy.skills import pack_skill

    if (source is None) == (archive is None):
        raise typer.BadParameter("Provide a source directory or --archive, exclusively")
    if archive is not None and request_id is None:
        raise typer.BadParameter("--archive retries require --request-id")
    if skill_id is not None and expected_revision is None:
        raise typer.BadParameter("Updates require --expected-revision")
    request_id = request_id or uuid4()
    temporary = None
    if source is not None:
        temporary = Path(tempfile.mkdtemp(prefix="kapy-skill-"))
        archive = temporary / "archive.zip"
        try:
            pack_skill(source, archive)
        except BaseException:
            shutil.rmtree(temporary)
            raise
    assert archive is not None
    params = transfer_params(ctx, archive, request_id)
    if skill_id is not None:
        params.update(skill_id=skill_id, expected_revision=expected_revision)
    try:
        invoke(ctx, "skill.update" if skill_id else "skill.create", params)
    except BaseException:
        typer.echo(
            f"Archive retained: {archive}; retry with --archive and --request-id {request_id}",
            err=True,
        )
        raise
    if temporary is not None:
        shutil.rmtree(temporary)


@skill.command("download")
def download_skill(
    ctx: typer.Context,
    skill_id: str,
    destination: Path,
    expected_revision: Annotated[int | None, typer.Option(min=1)] = None,
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    from kapy.skills import extract_skill

    with tempfile.TemporaryDirectory(prefix="kapy-skill-") as folder:
        archive = Path(folder) / "archive.zip"
        params = transfer_params(ctx, archive, request_id or uuid4())
        params.update(skill_id=skill_id, expected_revision=expected_revision)
        result = cast(dict[str, Any], asyncio.run(client(ctx).call("skill.download", params)))
        if (
            archive.stat().st_size != result["archive_bytes"]
            or archive.stat().st_size > 16 * 1024 * 1024
        ):
            raise typer.BadParameter("Downloaded archive size mismatch")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != result["sha256"]:
            raise typer.BadParameter("Downloaded archive hash mismatch")
        root = extract_skill(archive, destination)
        display({**result, "destination": str(root)})


@skill.command("delete")
def delete_skill(
    ctx: typer.Context,
    skill_id: str,
    expected_revision: Annotated[int, typer.Option(min=1)],
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    invoke(
        ctx,
        "skill.delete",
        {
            "skill_id": skill_id,
            "expected_revision": expected_revision,
            "request_id": rid(request_id),
        },
    )


def main() -> None:
    try:
        app()
    except RpcError as exc:
        typer.echo(
            json.dumps({"error": {"code": exc.code, "message": exc.message, "data": exc.data}}),
            err=True,
        )
        raise SystemExit(1) from None
    except RpcDisconnected, RpcTimeout, httpx2.HTTPError, OSError:
        typer.echo(
            "Connection failed; a mutation may have completed. Reuse its request ID.", err=True
        )
        raise SystemExit(1) from None


def read_json_file(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except OSError, ValueError:
        raise typer.BadParameter("Configuration file must contain a JSON object") from None


def provider_config(path: Path, key_file: Path | None, key_env: str | None) -> JsonObject:
    config = read_json_file(path)
    if key_file is not None and key_env is not None:
        raise typer.BadParameter("Use --key-file or --key-env exclusively")
    try:
        key = (
            key_file.read_text(encoding="utf-8").strip()
            if key_file
            else os.environ[key_env]
            if key_env
            else None
        )
    except OSError, KeyError:
        raise typer.BadParameter("The selected key source is unavailable") from None
    if key is not None:
        config["api_key"] = key
    return config


@provider.command("create")
def create_provider(
    ctx: typer.Context,
    config_file: Annotated[Path, typer.Option()],
    key_file: Annotated[Path | None, typer.Option()] = None,
    key_env: Annotated[str | None, typer.Option()] = None,
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    invoke(
        ctx,
        "provider.create",
        {**provider_config(config_file, key_file, key_env), "request_id": rid(request_id)},
    )


@provider.command("update")
def update_provider(
    ctx: typer.Context,
    provider_id: str,
    expected_revision: Annotated[int, typer.Option(min=1)],
    config_file: Annotated[Path, typer.Option()],
    key_file: Annotated[Path | None, typer.Option()] = None,
    key_env: Annotated[str | None, typer.Option()] = None,
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    invoke(
        ctx,
        "provider.update",
        {
            **provider_config(config_file, key_file, key_env),
            "provider_id": provider_id,
            "expected_revision": expected_revision,
            "request_id": rid(request_id),
        },
    )


@provider.command("get")
def get_provider(ctx: typer.Context, provider_id: str) -> None:
    invoke(ctx, "provider.get", {"provider_id": provider_id})


@provider.command("list")
def list_providers(ctx: typer.Context, after_id: str | None = None, limit: int = 100) -> None:
    invoke(ctx, "provider.list", {"after_id": after_id, "limit": limit})


@provider.command("delete")
def delete_provider(
    ctx: typer.Context,
    provider_id: str,
    expected_revision: Annotated[int, typer.Option(min=1)],
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    invoke(
        ctx,
        "provider.delete",
        {
            "provider_id": provider_id,
            "expected_revision": expected_revision,
            "request_id": rid(request_id),
        },
    )


@provider.command("discover")
def discover_models(
    ctx: typer.Context,
    provider_id: str,
    page_token: str | None = None,
    limit: int = 100,
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    invoke(
        ctx,
        "provider.discover",
        {
            "provider_id": provider_id,
            "page_token": page_token,
            "limit": limit,
            "request_id": rid(request_id),
        },
    )


@provider.command("models")
def provider_models(
    ctx: typer.Context, provider_id: str, after_id: str | None = None, limit: int = 100
) -> None:
    invoke(
        ctx, "provider.models", {"provider_id": provider_id, "after_id": after_id, "limit": limit}
    )


@provider_model.command("create")
def create_model(
    ctx: typer.Context,
    provider_id: str,
    name: str,
    defaults: str = "{}",
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    invoke(
        ctx,
        "provider.model.create",
        {
            "provider_id": provider_id,
            "name": name,
            "defaults": json.loads(defaults),
            "request_id": rid(request_id),
        },
    )


@provider_model.command("get")
def get_model(ctx: typer.Context, model_id: str) -> None:
    invoke(ctx, "provider.model.get", {"model_id": model_id})


@provider_model.command("update")
def update_model(
    ctx: typer.Context,
    model_id: str,
    expected_revision: Annotated[int, typer.Option(min=1)],
    defaults: str = "{}",
    request_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    invoke(
        ctx,
        "provider.model.update",
        {
            "model_id": model_id,
            "expected_revision": expected_revision,
            "defaults": json.loads(defaults),
            "request_id": rid(request_id),
        },
    )

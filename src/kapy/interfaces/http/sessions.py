"""Session HTTP composition and WebSocket transport; services own all queue/history logic.

Direct execution awaits Temporal start confirmation; queue/cancel APIs only persist
legacy inputs. Borrowed services outlive requests and WebSocket subscriptions.
"""

import asyncio
import logging
from contextlib import aclosing
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Response, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter
from starlette.websockets import WebSocketState

from kapy.agent_runner import HistoryMessage, OutputEvent
from kapy.control.sessions import (
    CreateSession,
    InputChannel,
    SessionInput,
    SessionRecord,
    SessionService,
    SubmitInput,
    UpdateSession,
)
from kapy.pagination import Page

from .dependencies import HistoryPage, LiveCursor, OffsetPage
from .errors import ControlRoute
from .types import CreateSessionAndSchedule, DurableRun, StartDurableRunner

_logger = logging.getLogger(__name__)
_output_adapter = TypeAdapter(list[OutputEvent])


def create_session_router(sessions: SessionService) -> APIRouter:
    router = APIRouter(route_class=ControlRoute)

    @router.post("/sessions", status_code=201, operation_id="create_session_and_schedule")
    async def create_session_and_schedule(data: CreateSessionAndSchedule) -> SessionRecord:
        session = await sessions.create_session(
            CreateSession.model_validate(data.model_dump(exclude={"input"}))
        )
        if data.input is not None:
            await sessions.submit_input(session.id, data.input)
        return session

    @router.get("/sessions", operation_id="list_sessions")
    async def list_sessions(
        page: OffsetPage, provider_id: UUID | None = None, model_name: str | None = None
    ) -> Page[SessionRecord]:
        return await sessions.list_sessions(
            provider_id=provider_id, model_name=model_name, **page.model_dump()
        )

    @router.get("/sessions/{session_id}", operation_id="get_session")
    async def get_session(session_id: UUID) -> SessionRecord:
        return await sessions.get_session(session_id)

    @router.patch("/sessions/{session_id}", operation_id="update_session")
    async def update_session(session_id: UUID, data: UpdateSession) -> SessionRecord:
        return await sessions.update_session(session_id, data)

    @router.post("/sessions/{session_id}/close", operation_id="close_session")
    async def close_session(session_id: UUID) -> SessionRecord:
        return await sessions.close_session(session_id)

    @router.post(
        "/sessions/{session_id}/inputs", status_code=202, operation_id="submit_input_and_schedule"
    )
    async def submit_input_and_schedule(session_id: UUID, data: SubmitInput) -> SessionInput:
        submission = await sessions.submit_input(session_id, data)
        return submission.input

    @router.get("/sessions/{session_id}/inputs", operation_id="read_inputs")
    async def read_inputs(
        session_id: UUID, channel: InputChannel = "queued"
    ) -> tuple[SessionInput, ...]:
        return await sessions.read_inputs(session_id, channel)

    @router.delete("/sessions/{session_id}/inputs/{input_id}", operation_id="delete_input")
    async def delete_input(session_id: UUID, input_id: Annotated[int, Path(gt=0)]) -> bool:
        return await sessions.delete_input(session_id, input_id)

    @router.post(
        "/sessions/{session_id}/runner", status_code=202, operation_id="start_durable_runner"
    )
    async def start_durable_runner(session_id: UUID, data: StartDurableRunner) -> DurableRun:
        handle = await sessions.start_durable_runner(session_id, user_prompt=data.user_prompt)
        assert handle.result_run_id is not None
        return DurableRun(workflow_id=handle.id, run_id=handle.result_run_id)

    @router.get("/sessions/{session_id}/runner", operation_id="is_runner_running")
    async def is_runner_running(session_id: UUID) -> bool:
        return await sessions.is_durable_runner_running(session_id)

    @router.post("/sessions/{session_id}/cancel", status_code=202, operation_id="request_cancel")
    async def request_cancel(session_id: UUID) -> Response:
        await sessions.request_cancel(session_id)
        return Response(status_code=202)

    @router.get("/sessions/{session_id}/cancel", operation_id="read_cancel")
    async def read_cancel(session_id: UUID) -> bool:
        return await sessions.read_cancel(session_id)

    @router.get("/sessions/{session_id}/history", operation_id="read_history")
    async def read_history(session_id: UUID, page: HistoryPage) -> Page[HistoryMessage]:
        return await sessions.read_history(session_id, **page.model_dump())

    @router.websocket("/sessions/{session_id}/live")
    async def live_ws(websocket: WebSocket, session_id: UUID, after_seq: LiveCursor) -> None:
        await websocket.accept()

        async def send() -> None:
            # The task that iterates owns generator cleanup, including idle disconnects.
            async with aclosing(sessions.live(session_id, after_seq=after_seq)) as batches:
                async for batch in batches:
                    await websocket.send_text(_output_adapter.dump_json(batch).decode("utf-8"))

        async def receive() -> None:
            message = await websocket.receive()
            if message["type"] != "websocket.disconnect":
                await websocket.close(code=1003)

        tasks = {asyncio.create_task(send()), asyncio.create_task(receive())}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        except Exception as error:
            _logger.error("Live failed for session %s: %s", session_id, type(error).__name__)
            if websocket.application_state == WebSocketState.CONNECTED:
                await websocket.close(code=1011)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if (
                websocket.application_state == WebSocketState.CONNECTED
                and websocket.client_state == WebSocketState.CONNECTED
            ):
                await websocket.close()

    return router

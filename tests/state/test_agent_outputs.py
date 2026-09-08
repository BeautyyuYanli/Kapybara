"""Runner output and compression across durable State runs."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx2
import pytest
from agent.test_runner import response, runner

from kapy.state import ReplyTo

from .conftest import Database, spec
from .test_events import waiting

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_waiting_input_survives_usage_compression_and_replies_after_restart(
    database: Database,
) -> None:
    channel = uuid4()
    question = "Which delivery includes the original question?"
    requests = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        direct = [
            value
            for message in body["messages"]
            if message["role"] == "user"
            and (value := json.loads(message["content"])).get("type") == "session_input"
        ]
        match len(requests):
            case 1:
                return response(name="process_list", call_id="earlier-tool")
            case 2:
                return response(
                    text="Earlier complete reply",
                    name="reply_to",
                    args={"ids": [direct[-1]["being_waited_id"]]},
                    call_id="earlier-reply",
                    tokens=800,
                )
            case 3:
                return response(
                    name="wait_for", args={"ids": [str(channel)]}, call_id="wait", tokens=800
                )
            case 4:
                return response(
                    text="The waiting result answers the original question",
                    name="reply_to",
                    args={"ids": [direct[-1]["being_waited_id"]]},
                    call_id="resumed-reply",
                )
            case _:
                raise AssertionError("no extra inference or correction should be needed")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        agent.config = replace(agent.config, context_window_tokens=1000, max_output_tokens=100)
        service = await database.start(agent)
        created = await service.create_session(
            replace(
                spec(),
                config={"output_mode": "reply_to"},
                initial_state=agent.initial_state(instructions="", skills=[]),
            ),
            request_id=uuid4(),
            input="Earlier answered question",
        )
        assert created.submission is not None
        first = await service.wait_submission(
            created.session.id, created.submission.request_id, wait_seconds=10
        )
        assert first.completion and first.completion.outcome == "completed"
        await waiting(database, created.session.id)
        pending = await service.submit_input(created.session.id, question, request_id=uuid4())
        async with asyncio.timeout(10):
            while not await database.rows(  # noqa: ASYNC110 - bounded DB observation
                "SELECT 1 FROM waiting_channels WHERE id=%s AND active", (channel,)
            ):
                await asyncio.sleep(0.01)
        assert (
            await service.wait_submission(created.session.id, pending.request_id)
        ).completion is None
        # Discard the service and Runner; the unresolved address and history must come from SQL.
        await service.__aexit__(None, None, None)
        resumed = runner(client)
        resumed.config = agent.config
        service = await database.start(resumed)
        await service.publish_event(
            channel, "dependency finished", request_id=uuid4(), producer_session_id=None
        )
        completed = await service.wait_submission(
            created.session.id, pending.request_id, wait_seconds=10
        )
        assert completed.completion and completed.completion.outcome == "completed"
        assert completed.completion.output == ReplyTo(
            (pending.waiting_id,), "The waiting result answers the original question"
        )

    assert len(requests) == 4
    projected = requests[-1]["messages"]
    original = {
        "type": "session_input",
        "being_waited_id": str(pending.waiting_id),
        "payload": question,
    }
    assert any(
        message["role"] == "user" and json.loads(message["content"]) == original
        for message in projected
    )
    instructions = next(
        message["content"]
        for message in projected
        if "Read inputs awaiting a reply (being_waited_id):" in str(message.get("content"))
    )
    assert str(pending.waiting_id) in instructions
    assert str(created.submission.waiting_id) not in instructions
    assert "dependency finished" in json.dumps(projected)
    old_output = {
        "kind": "reply_to",
        "being_waited_ids": [str(created.submission.waiting_id)],
        "payload": "Earlier complete reply",
    }
    assert any(
        message["role"] == "assistant"
        and (message.get("content") or "").startswith("{")
        and json.loads(message["content"]) == old_output
        for message in projected
    )
    saved = (
        await database.rows(
            "SELECT r.runner_state FROM runs r JOIN sessions s ON r.id=s.latest_run_id "
            "WHERE s.id=%s",
            (created.session.id,),
        )
    )[0]["runner_state"]["data"]
    # Two fresh high-usage responses compressed the answered cycle.
    # The pending question's cycle remained intact.
    assert [cycle["level"] for cycle in saved["cycles"]] == [2, 0, 0]

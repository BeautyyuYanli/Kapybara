import json
from dataclasses import replace
from uuid import uuid4

import httpx2
import pytest
from pydantic import SecretStr, ValidationError
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart

from kapy.agent import OpenAICompatibleBackend, ProcessCommand, ScriptHost, ScriptTool
from kapy.agent.machine import MachineTools, Wait
from kapy.agent.runner import Runtime
from kapy.skills import SkillDescription

from .test_machine import EchoArgs, FilesCaller
from .test_runner import Caller, Context, response, runner


@pytest.mark.asyncio
async def test_compatible_backend_preserves_endpoint_key_model_and_borrowed_client():
    calls = []

    async def handle(request):
        calls.append(request)
        return response(text="compatible reply")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        backend = OpenAICompatibleBackend(
            base_url="https://compatible.invalid/custom/v1",
            api_key=SecretStr("endpoint-key"),
            http_client=client,
        )
        agent = runner(client, plugins=())
        agent.config = replace(agent.config, model="vendor/family-17b")
        agent.model_backend = backend
        ctx = Context(agent.initial_state(instructions="original", skills=[]))
        result = await agent(ctx)
        assert result.output == "compatible reply" and not client.is_closed
        assert str(calls[0].url) == "https://compatible.invalid/custom/v1/chat/completions"
        assert calls[0].headers["authorization"] == "Bearer endpoint-key"
        assert json.loads(calls[0].content)["model"] == "vendor/family-17b"
        error = ModelHTTPError(
            400,
            "vendor/family-17b",
            {
                "error": "unsupported image endpoint-key https://example.test/file?secret=hidden "
                + "a" * 200
            },
        )
        failure = backend.classify_error(error)
        assert failure is not None and failure.kind == "media"
        assert "endpoint-key" not in failure.message and "hidden" not in failure.message
        assert "a" * 200 not in failure.message
        assert (
            backend.classify_error(ModelHTTPError(429, "x", {"error": "unsupported image"})) is None
        )
        assert (
            backend.classify_error(ModelHTTPError(400, "x", {"error": "invalid parameter"})) is None
        )


@pytest.mark.asyncio
async def test_actual_model_instructions_schema_and_current_context_keep_snapshot():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return response(text="done")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client, plugins=())
        state = agent.initial_state(
            instructions="Saved user instruction",
            skills=[SkillDescription("skill-x", "Saved skill")],
        )
        original = json.dumps(state.data, sort_keys=True)
        ctx = Context(state)
        ctx.session = replace(
            ctx.session, machine_ids=("box-a", "box-b"), default_machine_id="box-b"
        )
        await agent(ctx)
        assert json.dumps(state.data, sort_keys=True) == original
        body = requests[0]
        instructions = "\n".join(
            m["content"] for m in body["messages"] if m["role"] in {"system", "developer"}
        )
        assert "Saved user instruction" in instructions and "Saved skill" in instructions
        assert str(ctx.session.id) in instructions and "box-a, box-b" in instructions
        assert "Default machine: box-b" in instructions and "submission.waiting_id" in instructions
        tools = {tool["function"]["name"]: tool["function"] for tool in body["tools"]}
        assert not {"apply_patch", "file_read", "file_write"} & tools.keys()
        schema = tools["process_wait"]["parameters"]
        assert "next byte" in tools["process_start"]["description"]
        assert "Ctrl-C" in tools["process_write"]["description"]
        assert schema["properties"]["machine_id"]["description"]
        alternatives = schema["properties"]["cursor"]["anyOf"]
        objects = [
            schema["$defs"][item["$ref"].rsplit("/", 1)[1]] if "$ref" in item else item
            for item in alternatives
        ]
        assert any(set(item.get("properties", {})) == {"pty"} for item in objects)
        assert any(set(item.get("properties", {})) == {"stdout", "stderr"} for item in objects)
        assert set(tools["wait"]["parameters"]["properties"]) == {"wait_for"}


@pytest.mark.parametrize(
    "cursor", [{"pty": True}, {"pty": -1}, {"stdout": 0}, {"pty": 0, "stderr": 0}]
)
def test_cursor_validation_rejects_unusable_shape(cursor):
    with pytest.raises(ValidationError):
        Wait.model_validate({"process_id": str(uuid4()), "cursor": cursor})


@pytest.mark.asyncio
async def test_removed_tool_recovery_records_unknown_without_machine_action():
    caller = Caller()
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return response(text="I will inspect before changing anything")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client, caller, plugins=())
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        runtime = Runtime(agent, ctx, "test")
        await runtime.initialize()
        await runtime.record(
            ModelResponse(
                [ToolCallPart("file_write", {"path": "file", "content": "x"}, "old-call")]
            )
        )
        runtime.data["pending_tools"] = [
            {"tool_call_id": "old-call", "steps": [{"method": "file.push"}]}
        ]
        await runtime.checkpoint()
        result = await agent(ctx)
        assert result.output == "I will inspect before changing anything" and caller.calls == []
        returned = next(m for m in requests[0]["messages"] if m.get("tool_call_id") == "old-call")
        assert (
            "outcome_unknown" in returned["content"]
            and "No operation was repeated" in returned["content"]
        )
        assert not result.checkpoint.state.data["pending_tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["custom_prepare", "apply_patch"])
async def test_generic_prepare_can_replace_argv_and_transfer_without_name_dispatch(name):
    caller = FilesCaller()
    seen = []

    async def prepare(host: ScriptHost, command: ProcessCommand) -> ProcessCommand:
        directory = await host.workspace()
        await host.run(("mkdir", "-p", directory + "/tools"))
        await host.push(directory + "/tools/input", b"resource")
        seen.append(command)
        return replace(command, argv=("prepared-executable",))

    plugin = ScriptTool(
        name,
        "Prepared script",
        EchoArgs,
        lambda args: ProcessCommand(("original",), stdin=args.value.encode()),
        prepare,
    )
    async with httpx2.AsyncClient() as client:
        agent = runner(client, caller, plugins=(plugin,))
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        runtime = Runtime(agent, ctx, "test")
        await runtime.initialize()
        value = await MachineTools(runtime, caller, (plugin,)).execute(
            name, {"value": "patch", "repeat": 1}, "prepare-call"
        )
        assert value["process"]["exit_code"] == 0
    assert seen[0].argv == ("original",)
    assert caller.uploads["/session/tools/input"] == b"resource"
    assert any(command[-1] == "prepared-executable" for command in caller.commands)


@pytest.mark.asyncio
async def test_preparation_unknown_retains_handle_and_never_launches_script():
    caller = FilesCaller()
    caller.running = True

    async def prepare(host: ScriptHost, command: ProcessCommand) -> ProcessCommand:
        await host.run(("/bin/sh", "-c", "unfinished preparation"))
        return command

    plugin = ScriptTool(
        "prepare_unknown",
        "Prepared script",
        EchoArgs,
        lambda args: ProcessCommand(("must-not-run",)),
        prepare,
    )
    async with httpx2.AsyncClient() as client:
        agent = runner(client, caller, plugins=(plugin,))
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        runtime = Runtime(agent, ctx, "test")
        await runtime.initialize()
        result = await MachineTools(runtime, caller, (plugin,)).execute(
            "prepare_unknown", {"value": "unused", "repeat": 1}, "prepare-unknown"
        )
    assert result["error"] == "outcome_unknown"
    assert caller.calls[0][2]["process_id"] in result["message"]
    assert caller.commands == [["/bin/sh", "-c", "unfinished preparation"]]

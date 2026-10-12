"""Deterministic SDK streaming model with configurable native tool calls.

Subclass the configured protocol class so normal model settings validation and
resource contexts are exercised. Only model generation is replaced; there are
no external model HTTP requests. JSON prompts describe sequential request steps.
"""

import json
from contextlib import asynccontextmanager

from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel


class _ScriptedModel(TestModel):
    def _request(self, messages, model_settings, model_request_parameters):
        assert {tool.name for tool in model_request_parameters.function_tools} == {
            "builtin_shell_run",
            "builtin_shell_wait",
            "builtin_shell_input",
            "builtin_shell_interrupt",
        }
        last_prompt = max(
            index
            for index, message in enumerate(messages)
            if any(isinstance(part, UserPromptPart) for part in message.parts)
        )
        prompt = next(
            part.content for part in messages[last_prompt].parts if isinstance(part, UserPromptPart)
        )
        assert isinstance(prompt, str)
        spec = json.loads(prompt)
        returns = [
            part
            for message in messages[last_prompt + 1 :]
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        steps = spec["steps"]
        completed = 0
        for step in steps:
            if completed == len(returns):
                return ModelResponse(
                    parts=[ToolCallPart(call["tool"], call["args"]) for call in step]
                )
            completed += len(step)
        if spec.get("fail"):
            raise UserError("deliberate model failure after tools")
        return ModelResponse(parts=[TextPart(json.dumps([part.content for part in returns]))])


class LayerModel(OpenAIChatModel):
    def __init__(self, model_name, *, provider, profile=None):
        super().__init__(model_name, provider=provider, profile=profile)
        self._scripted = _ScriptedModel()

    @asynccontextmanager
    async def request_stream(
        self, messages, model_settings, model_request_parameters, run_context=None
    ):
        async with self._scripted.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream

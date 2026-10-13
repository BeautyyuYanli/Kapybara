"""Deterministic streaming model calls the MCP tool discovered through the real layer."""

import re
from contextlib import asynccontextmanager

from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel


class _MCPModel(TestModel):
    def _request(self, messages, model_settings, model_request_parameters):
        latest = max(
            index
            for index, message in enumerate(messages)
            if any(isinstance(part, UserPromptPart) for part in message.parts)
        )
        returned = [
            part
            for message in messages[latest + 1 :]
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if len(returned) >= 2:
            return ModelResponse(parts=[TextPart(str(returned[-1].content))])
        assert all(
            re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name)
            for tool in model_request_parameters.function_tools
        )
        tool = next(
            tool for tool in model_request_parameters.function_tools if "_echo_" in tool.name
        )
        assert tool.capability_id == "mcps"
        return ModelResponse(parts=[ToolCallPart(tool.name, {"text": "durable"})])


class MCPsModel(OpenAIChatModel):
    def __init__(self, model_name, *, provider, profile=None):
        super().__init__(model_name, provider=provider, profile=profile)
        self.scripted = _MCPModel()

    @asynccontextmanager
    async def request_stream(
        self, messages, model_settings, model_request_parameters, run_context=None
    ):
        async with self.scripted.request_stream(
            messages,
            model_settings,
            model_request_parameters,
            run_context,
        ) as stream:
            yield stream

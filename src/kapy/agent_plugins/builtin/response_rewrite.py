"""Rewrite completed business text before history/checkpoint with an independent API.

The execution owns one client, with no session resources or run-local state. Only
visible text leaves this boundary: history, thinking and main-model configuration
are never passed to the rewriter. Failed rewrites preserve the original response;
cancellation propagates. Uncommitted requests can repeat after runner recovery.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import anyio
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart
from pydantic_ai.models import ModelRequestContext

from kapy.agent_plugins.contracts import AgentPlugin, PluginBinding, SessionContext
from kapy.agent_plugins.registry import PluginDefinition

logger = logging.getLogger(__name__)
REWRITE_TIMEOUT_SECONDS = 60
CLEANUP_TIMEOUT_SECONDS = 5


class ResponseRewriteConfig(BaseModel):
    """Pure validation; the key is hidden in repr but persists as ordinary JSON."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    base_url: HttpUrl
    api_key: str = Field(repr=False)

    @field_validator("prompt", "api_key")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must contain non-whitespace text")
        return value

    @field_validator("base_url")
    @classmethod
    def api_root(cls, value: HttpUrl) -> HttpUrl:
        if any(
            part is not None
            for part in (value.username, value.password, value.query, value.fragment)
        ):
            raise ValueError("base_url must not contain credentials, query or fragment")
        return value


class ResponseRewriteState(BaseModel):
    """No durable state or session-owned resources."""

    model_config = ConfigDict(extra="forbid")


class ResponseRewriteCapability(AbstractCapability[Any]):
    """Stateless per-request hook borrowing its execution's client and prompt."""

    def __init__(self, client: AsyncOpenAI, prompt: str) -> None:
        self.client, self.prompt = client, prompt

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        if (
            (ctx.metadata or {}).get("kapy_run_kind") == "auxiliary"
            or request_context.model_request_parameters.output_mode != "text"
            or response.state != "complete"
            or response.finish_reason not in (None, "stop")
            or any(not isinstance(part, TextPart | ThinkingPart) for part in response.parts)
        ):
            return response
        original = response.text
        if original is None or not original.strip():
            return response

        try:
            with anyio.fail_after(REWRITE_TIMEOUT_SECONDS):
                completion = await self.client.chat.completions.create(
                    model="gemini-3.8-flash",
                    messages=[
                        {"role": "system", "content": self.prompt},
                        {"role": "user", "content": original},
                    ],
                    stream=False,
                )
                # The SDK normally constructs response objects without validation.
                # Validate the actual wire fields before accepting remote content.
                completion = ChatCompletion.model_validate(
                    completion.model_dump(warnings=False), strict=True
                )
                if len(completion.choices) != 1:
                    raise ValueError("unusable completion")
                choice = completion.choices[0]
                message = choice.message
                if extra := message.model_extra:
                    # Gemini's plain-text replies can include this opaque metadata.
                    # Accept only that documented shape, not arbitrary media fields.
                    extra_content = extra.get("extra_content")
                    google = (
                        extra_content.get("google") if isinstance(extra_content, dict) else None
                    )
                    signature = (
                        google.get("thought_signature") if isinstance(google, dict) else None
                    )
                    if not isinstance(signature, str) or extra != {
                        "extra_content": {"google": {"thought_signature": signature}}
                    }:
                        raise ValueError("unsupported completion metadata")
                if (
                    choice.finish_reason != "stop"
                    or message.refusal is not None
                    or message.tool_calls
                    or message.function_call is not None
                    or message.audio is not None
                    or not message.content
                    or not message.content.strip()
                ):
                    raise ValueError("unusable completion")
                rewritten = message.content
        except Exception as error:
            logger.warning("Response rewrite failed (%s)", type(error).__name__)
            return response

        parts: list[TextPart | ThinkingPart] = []
        inserted = False
        for part in response.parts:
            if isinstance(part, ThinkingPart):
                parts.append(part)
            elif not inserted:
                parts.append(TextPart(rewritten))
                inserted = True
        return replace(response, parts=parts)


class ResponseRewritePlugin(AgentPlugin[ResponseRewriteConfig, ResponseRewriteState]):
    @asynccontextmanager
    async def open_execution(
        self, ctx: SessionContext[ResponseRewriteConfig, ResponseRewriteState]
    ) -> AsyncIterator[PluginBinding]:
        client = AsyncOpenAI(
            base_url=str(ctx.config.base_url),
            api_key=ctx.config.api_key,
            max_retries=0,
            timeout=REWRITE_TIMEOUT_SECONDS,
            # A redirect would replay the completion POST despite max_retries=0.
            http_client=DefaultAsyncHttpxClient(follow_redirects=False),
        )
        try:
            yield PluginBinding(
                capabilities=(ResponseRewriteCapability(client, ctx.config.prompt),)
            )
        finally:
            # Propagation retains any active execution failure in the exception chain.
            with anyio.fail_after(CLEANUP_TIMEOUT_SECONDS, shield=True):
                await client.close()


RESPONSE_REWRITE_PLUGIN = PluginDefinition(
    plugin_provider="builtin",
    plugin_name="response_rewrite",
    data_version=1,
    config_type=ResponseRewriteConfig,
    state_type=ResponseRewriteState,
    plugin_type=ResponseRewritePlugin,
)

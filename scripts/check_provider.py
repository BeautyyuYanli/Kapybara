"""Check the configured model's tool protocol without executing machine commands."""

import json
import os
import uuid

import anyio
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits


async def main() -> None:
    marker = "KAPY-" + uuid.uuid4().hex[:12]
    calls: list[str] = []
    provider = OpenAIProvider(
        base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"]
    )
    agent = Agent(
        OpenAIChatModel(os.environ["OPENAI_MODEL"], provider=provider),
        model_settings={"max_tokens": 256},
    )

    @agent.tool_plain
    def read_test_marker() -> str:
        """Read the test marker. Call once to discover its value."""
        calls.append(marker)
        return marker

    with anyio.fail_after(60):
        result = await agent.run(
            "Call read_test_marker exactly once, then reply with exactly its returned value.",
            usage_limits=UsageLimits(request_limit=3),
        )
    ok = len(calls) == 1 and result.output.strip() == marker
    print(
        json.dumps(
            {
                "model": os.environ["OPENAI_MODEL"],
                "tool_calls": len(calls),
                "requests": result.usage.requests,
                "reply_matches_tool_result": ok,
            }
        )
    )
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    anyio.run(main)

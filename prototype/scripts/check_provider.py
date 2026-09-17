"""Check the configured model's tool protocol without executing machine commands."""

import argparse
import json
import uuid

import anyio
from model_selection import selected_model
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits


async def main(model_id: str) -> None:
    marker = "KAPY-" + uuid.uuid4().hex[:12]
    calls: list[str] = []
    async with selected_model(model_id) as (backend, name, window, output):
        agent = Agent(backend.create_model(name), model_settings={"max_tokens": min(256, output)})

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
                    "model_id": model_id,
                    "tool_calls": len(calls),
                    "requests": result.usage.requests,
                    "reply_matches_tool_result": ok,
                }
            )
        )
        if not ok:
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    anyio.run(main, parser.parse_args().model_id)

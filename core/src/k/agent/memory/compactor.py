"""Memory compaction helpers.

This module renders `pydantic_ai` `ModelRequest`/`ModelResponse` traces into a
compact, mostly-text representation suitable for LLM-based "memory compaction".

Key invariant: large payloads (bytes/base64/binary) must be omitted (or replaced
with small placeholders) to avoid bloating the compaction prompt.

Multimodal detection is intentionally type-driven via `pydantic_ai`'s
multimodal types and their `kind` discriminator.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from typing import cast

from pydantic_ai import Agent, ToolOutput
from pydantic_ai.messages import (
    AudioUrl,
    BaseToolCallPart,
    BaseToolReturnPart,
    BinaryContent,
    DocumentUrl,
    FilePart,
    FileUrl,
    ImageUrl,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    UserPromptPart,
    VideoUrl,
)
from pydantic_ai.models import KnownModelName, Model


async def result(
    compacted: list[str],
) -> list[str]:
    return compacted


_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")


def _looks_like_base64(value: str) -> bool:
    """Return True when `value` is likely a base64 or base64-data-url payload."""

    if value.startswith("data:") and ";base64," in value:
        return True

    # Keep ordinary URLs; they are useful trace references.
    if value.startswith(("http://", "https://")):
        return False

    if len(value) < 128:
        return False
    if any(ch.isspace() for ch in value):
        return False
    if not _BASE64_RE.fullmatch(value):
        return False

    # Base64 strings are typically length % 4 != 1 (url-safe variants may omit padding).
    return len(value) % 4 != 1


def _multimodal_mapping_url_or_binary(
    value: Mapping[str, object],
) -> tuple[str | None, bool]:
    """Return (url, is_binary) for pydantic-ai style multimodal mappings.

    This intentionally avoids deep validation: we only need to distinguish
    URL-based multimodal payloads (keep `url`) from binary payloads (omit).
    """

    kind = value.get("kind")
    if not isinstance(kind, str):
        return (None, False)

    if kind == "binary":
        return (None, True)

    if kind in {"image-url", "audio-url", "video-url", "document-url", "file-url"}:
        url = value.get("url")
        return (url, False) if isinstance(url, str) and url else (None, False)

    return (None, False)


def _url_from_multimodal(value: object) -> str | None:
    if isinstance(value, (AudioUrl, DocumentUrl, ImageUrl, VideoUrl)):
        return value.url
    if isinstance(value, FileUrl):
        return value.url
    if isinstance(value, Mapping):
        url, is_binary = _multimodal_mapping_url_or_binary(
            cast(Mapping[str, object], value)
        )
        return None if is_binary else url
    return None


compact_agent = Agent(
    system_prompt="""You are a memory compactor.

## Goal
Convert the provided conversation/tool traces into a chronological list of concise, reusable steps that preserve high-fidelity operational details.

## High-fidelity rule (most important)
Do **not** over-summarize away the specifics of what the agent:
- received (inputs/constraints/context),
- tried (actions, commands, edits, tool calls),
- observed (tool outputs, errors, test results, confirmations),
- responded (messages delivered to the user and artifacts produced).

Include failed attempts when they influenced the next step (briefly: what was tried, what went wrong, what changed).

## Output format
- Return a list of strings by the `result` tool.
- Do not require any fixed per-line prefix, but each line must be unambiguous about who did what (user intent vs agent action).
- Prefer dense step-lines that explicitly capture: Received → Tried → Observed → Responded (omit segments that truly don't apply).

## What to keep (optimize for reuse)
- Preserve the full task arc: request → investigation → execution → verification → result.
- Keep one major step per line; merge noisy sub-steps that share the same purpose.
- Prefer concrete, action-oriented phrasing: what was done, why it mattered, and the outcome.
- Keep details that help someone repeat the work later:
  - tool/skill names, key flags/options, file paths (e.g. `/tmp/...`), IDs (e.g. chat_id), chosen models/voices, extracted facts/results, and verification signals.
- Keep user-provided specifics that drive correctness (constraints, examples, acceptance criteria, snippets of inputs/outputs). Quote short fragments when useful; do not paste long payloads.
- Drop filler that doesn't affect decisions or outcomes (chit-chat, apologies, self-talk, repeated instructions).

## Skills (special rule)
- If the trace shows the agent reading or relying on a skill doc (`SKILLS.md`), include a short, task-relevant excerpted summary of the skill instructions and the skill path.
  - Summarize in one line per skill (do not paste the whole doc).
  - Keep only the parts that were relevant to the current task (what was actually used or needed), but include enough to reuse that subset: what it does, required inputs/env vars if mentioned, and the canonical command/API shape.
  - Example: `Reviewed skills:messager/telegram/SKILLS.md: sendMessage via Bot API; needs TELEGRAM_BOT_TOKEN; requires chat_id`

## Tool/command representation
- Keep commands readable and actionable. Keep full URLs (including paths/query strings) when they help trace the step; shorten truly huge non-URL payloads/outputs with "...".
- Do not include secrets or raw tokens. Redact them as `$ENV_VAR`, `<REDACTED>`, or "...", including when they appear inside a URL.
- Avoid dumping raw tool logs, stack traces, or large structured blobs; summarize the intent + result instead.
""",
    output_type=ToolOutput(result, name="result"),
)


def print_detailed(detailed: list[ModelRequest | ModelResponse]) -> str:
    def text_only_content(value: object) -> tuple[str | None, dict[str, int]]:
        omitted: dict[str, int] = {}

        def bump(key: str) -> None:
            omitted[key] = omitted.get(key, 0) + 1

        def walk(v: object) -> list[str]:
            if v is None:
                return []
            if isinstance(v, str):
                if _looks_like_base64(v):
                    bump("base64")
                    return []
                return [v]
            if isinstance(v, (int, float, bool)):
                return [str(v)]
            if isinstance(v, (bytes, bytearray, memoryview)):
                bump("binary")
                return []
            if isinstance(v, BinaryContent):
                bump("binary")
                return []
            url = _url_from_multimodal(v)
            if url:
                return [url]
            if dataclasses.is_dataclass(v) and not isinstance(v, type):
                try:
                    return walk(dataclasses.asdict(v))
                except Exception:
                    bump(type(v).__name__)
                    return []
            if hasattr(v, "model_dump"):
                try:
                    dumped = v.model_dump()  # type: ignore[no-any-return]
                except Exception:
                    bump(type(v).__name__)
                    return []
                return walk(dumped)
            if isinstance(v, dict):
                v_dict = cast(dict[str, object], v)

                url, is_binary = _multimodal_mapping_url_or_binary(v_dict)
                if is_binary:
                    bump("binary")
                    return []
                if url:
                    return [url]

                out: list[str] = []
                for k, vv in v_dict.items():
                    if not isinstance(k, str) or not k:
                        continue
                    vv_text = walk(vv)
                    if not vv_text:
                        continue
                    if len(vv_text) == 1:
                        out.append(f"{k}={vv_text[0]}")
                    else:
                        out.append(f"{k}=" + "\n".join(vv_text))
                return out
            if isinstance(v, (list, tuple, set)):
                out = []
                for item in v:
                    out.extend(walk(item))
                return out

            bump(type(v).__name__)
            return []

        text_parts = walk(value)
        text = "\n".join(x for x in text_parts if x.strip())
        return (text or None), omitted

    def _tool_return_payload(part: BaseToolReturnPart) -> object:
        content = getattr(part, "content", None)
        if dataclasses.is_dataclass(content) and hasattr(content, "content"):
            return content.content
        return content

    def _part_text_or_placeholder(
        *,
        content: object,
        placeholder_prefix: str,
    ) -> str:
        text, omitted = text_only_content(content)
        if text:
            return text
        if omitted:
            omitted_str = ", ".join(f"{k} x{v}" for k, v in sorted(omitted.items()))
            return f"<{placeholder_prefix}: {omitted_str}>"
        return ""

    def _sanitize_for_repr(value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            return "<base64 omitted>" if _looks_like_base64(value) else value
        if isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "<binary omitted>"
        if isinstance(value, BinaryContent):
            return "<binary omitted>"
        url = _url_from_multimodal(value)
        if url:
            return url
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            try:
                return _sanitize_for_repr(dataclasses.asdict(value))
            except Exception:
                return f"<{type(value).__name__} omitted>"
        if hasattr(value, "model_dump"):
            try:
                dumped = value.model_dump()  # type: ignore[no-any-return]
            except Exception:
                return f"<{type(value).__name__} omitted>"
            return _sanitize_for_repr(dumped)
        if isinstance(value, dict):
            value_dict = cast(dict[str, object], value)
            url, is_binary = _multimodal_mapping_url_or_binary(value_dict)
            if is_binary:
                return "<binary omitted>"
            if url:
                return url
            return {
                k: _sanitize_for_repr(v)
                for k, v in value_dict.items()
                if isinstance(k, str)
            }
        if isinstance(value, (list, tuple, set)):
            return [_sanitize_for_repr(v) for v in value]
        return f"<{type(value).__name__} omitted>"

    res = ""
    for msg in detailed:
        if isinstance(msg, ModelRequest):
            inbound_parts: list[object] = []
            for part in msg.parts:
                if isinstance(part, BaseToolReturnPart):
                    text = _part_text_or_placeholder(
                        content=_tool_return_payload(part),
                        placeholder_prefix="non-text tool output omitted",
                    )
                    inbound_parts.append(
                        {
                            "tool_name": part.tool_name,
                            "content": text,
                            "tool_call_id": part.tool_call_id,
                        }
                    )
                elif isinstance(part, UserPromptPart):
                    inbound_parts.append(
                        _part_text_or_placeholder(
                            content=part.content,
                            placeholder_prefix="non-text user content omitted",
                        )
                    )
                elif isinstance(part, SystemPromptPart):
                    inbound_parts.append(part.content)
                elif isinstance(part, RetryPromptPart):
                    inbound_parts.append(
                        _part_text_or_placeholder(
                            content=part.content,
                            placeholder_prefix="non-text retry content omitted",
                        )
                    )
                else:
                    inbound_parts.append(
                        _part_text_or_placeholder(
                            content=getattr(part, "content", part),
                            placeholder_prefix="non-text content omitted",
                        )
                    )
            res += f"Inbound: {inbound_parts!r}\n"
        else:
            assistant_parts: list[object] = []
            for part in msg.parts:
                if isinstance(part, BaseToolCallPart):
                    assistant_parts.append(
                        {
                            "tool_name": part.tool_name,
                            "args": _sanitize_for_repr(part.args),
                            "tool_call_id": part.tool_call_id,
                        }
                    )
                elif isinstance(part, FilePart):
                    assistant_parts.append("<file omitted>")
                elif isinstance(part, (TextPart, ThinkingPart)):
                    assistant_parts.append(part.content)
                else:
                    assistant_parts.append(
                        _sanitize_for_repr(getattr(part, "content", part))
                    )

            res += f"Assistant: {assistant_parts!r}\n"
    return res


async def run_compaction(
    model: Model | KnownModelName,
    detailed: list[ModelRequest | ModelResponse],
):
    compacted = await compact_agent.run(
        model=model,
        user_prompt=print_detailed(detailed),
    )
    return compacted.output


async def main():
    from rich import print

    with open("mem.jsonl", encoding="utf-8") as f:
        lines = f.read()

    res = await compact_agent.run(
        model="openai:gpt-5-chat-latest",
        user_prompt=lines,
    )
    print(res.output)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

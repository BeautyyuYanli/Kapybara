"""Explicit session model connections; borrowed SDK transports and safe failures."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx2
from pydantic import SecretStr
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider


@dataclass(frozen=True, slots=True)
class ModelFailure:
    kind: Literal["context_length", "media"]
    message: str


class ModelBackend(Protocol):
    """Borrowed adapter; failure messages must be safe to persist and display."""

    def create_model(self, model_name: str) -> Model: ...
    def classify_error(self, error: Exception) -> ModelFailure | None: ...


class OpenAICompatibleBackend:
    """Chat Completions transport; never owns or closes the injected HTTP client."""

    def __init__(
        self, *, base_url: str, api_key: SecretStr, http_client: httpx2.AsyncClient
    ) -> None:
        self.provider = OpenAIProvider(
            base_url=base_url, api_key=api_key.get_secret_value(), http_client=http_client
        )
        self._api_key = api_key

    def create_model(self, model_name: str) -> Model:
        return OpenAIChatModel(model_name, provider=self.provider)

    def classify_error(self, error: Exception) -> ModelFailure | None:
        status = getattr(error, "status_code", None)
        raw = str(getattr(error, "body", error))
        lower = raw.lower()
        if status in (400, 413, 422) and any(
            code in lower
            for code in (
                "context_length_exceeded",
                "context window",
                "maximum context length",
                "too many tokens",
            )
        ):
            return ModelFailure("context_length", "Provider rejected context length")
        media_evidence = any(
            term in lower
            for term in ("image", "audio", "video", "media", "file_data", "file content", "pdf")
        ) and any(
            term in lower for term in ("invalid", "unsupported", "decode", "format", "not support")
        )
        if not media_evidence or not (
            status in (400, 422) or isinstance(error, (ValueError, NotImplementedError))
        ):
            return None
        key = self._api_key.get_secret_value()
        safe = raw.replace(key, "[redacted]") if key else raw
        safe = re.sub(r"https?://[^\s?'\"]+\?[^\s'\"]+", "[URL query redacted]", safe)
        safe = re.sub(r"(?i)(bearer\s+|api[_-]?key[=: ]+)[^\s,}\"']+", "[redacted]", safe)
        safe = re.sub(r"[A-Za-z0-9+/=_-]{100,}", "[large data redacted]", safe)
        return ModelFailure("media", safe.encode()[:8192].decode("utf-8", errors="ignore"))


type ModelType = Literal["openai_responses", "openai_chat", "google_ai_studio"]


@dataclass(frozen=True)
class ModelConnection:
    type: ModelType
    base_url: str
    api_key: SecretStr


class OpenAIResponsesBackend(OpenAICompatibleBackend):
    # The SDK needs reasoning item IDs to replay encrypted_content with full local
    # history. IDs identify supplied items; store=False forbids server-history reliance.
    def create_model(self, model_name: str) -> Model:
        return OpenAIResponsesModel(
            model_name,
            provider=self.provider,
            settings=OpenAIResponsesModelSettings(
                openai_store=False, openai_send_reasoning_ids=True, openai_truncation="disabled"
            ),
        )


class GoogleStudioBackend:
    def __init__(self, config: ModelConnection, http_client: httpx2.AsyncClient) -> None:
        assert config.api_key is not None
        self.provider = GoogleProvider(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            http_client=http_client,
        )

    def create_model(self, model_name: str) -> Model:
        return GoogleModel(model_name, provider=self.provider)

    def classify_error(self, error: Exception) -> ModelFailure | None:
        # google-genai APIError carries .code/.message; Pydantic ModelHTTPError
        # carries .status_code/.body. Neither raw message is persisted.
        status = getattr(error, "status_code", getattr(error, "code", None))
        raw = str(getattr(error, "body", getattr(error, "message", ""))).lower()
        if status in (400, 413) and (
            "input token count" in raw
            and "maximum" in raw
            or "context" in raw
            and ("limit" in raw or "too long" in raw)
        ):
            return ModelFailure("context_length", "Provider rejected context length")
        if (
            status in (400, 422)
            and any(term in raw for term in ("image", "audio", "video", "mime"))
            and any(term in raw for term in ("unsupported", "invalid", "decode"))
        ):
            return ModelFailure("media", "Provider rejected supplied media")
        return None


type ModelBackendFactory = Callable[[ModelConnection, httpx2.AsyncClient], ModelBackend]


def create_model_backend(config: ModelConnection, http_client: httpx2.AsyncClient) -> ModelBackend:
    if not config.api_key.get_secret_value() or not config.base_url:
        raise ValueError("A resolved model connection with an explicit key is required")
    if config.type == "google_ai_studio":
        return GoogleStudioBackend(config, http_client)
    backend = (
        OpenAIResponsesBackend if config.type == "openai_responses" else OpenAICompatibleBackend
    )
    return backend(base_url=config.base_url, api_key=config.api_key, http_client=http_client)

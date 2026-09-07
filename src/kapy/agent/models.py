"""Model construction and safe failure classification for compatible chat endpoints."""

import re
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx2
from pydantic import SecretStr
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
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

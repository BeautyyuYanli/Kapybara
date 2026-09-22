"""SDK construction and discovery outside database transactions.

Callers own Provider and Model contexts. Their exits have bounded cancellation
protection in the calling task, after nested graph/plugin scopes have unwound.
Enter the provider before constructing its model so constructor failure still
closes SDK-owned clients.
Class references select installed code, not a universal protocol adapter.
"""

import inspect
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from types import TracebackType
from typing import Any, cast

import anyio
from google.genai import Client
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, ImportString, TypeAdapter
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIChatModelSettings,
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.profiles import ModelProfileSpec
from pydantic_ai.providers import Provider

from kapy.control.types import JsonObject

from .models import ModelRow
from .types import CreateModel, ModelDiscoveryError, ProviderConfig

_PROVIDER_CLASS = TypeAdapter(ImportString[type[Provider]])
_MODEL_CLASS = TypeAdapter(ImportString[type[Model]])


class SettingsInput[SettingsT](BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")
    settings: SettingsT


_SETTINGS_INPUTS = {
    OpenAIChatModel: SettingsInput[OpenAIChatModelSettings],
    OpenAIResponsesModel: SettingsInput[OpenAIResponsesModelSettings],
    GoogleModel: SettingsInput[GoogleModelSettings],
}


def resolve_classes(config: ProviderConfig) -> tuple[type[Provider], type[Model]]:
    for reference in (config.provider_class, config.model_class):
        module, separator, name = reference.partition(":")
        if not separator or not module or not name or ":" in name:
            raise ValueError("Class references must use module:ClassName")
    provider_cls = _PROVIDER_CLASS.validate_python(config.provider_class)
    model_cls = _MODEL_CLASS.validate_python(config.model_class)
    if inspect.isabstract(provider_cls) or inspect.isabstract(model_cls):
        raise ValueError("Provider and model classes must be concrete")
    return provider_cls, model_cls


def provider_arguments(config: ProviderConfig) -> dict[str, Any]:
    forbidden = {"api_key", "base_url", "client", "http_client", "openai_client"}
    if forbidden.intersection(config.provider_kwargs):
        raise ValueError("provider_kwargs cannot override credentials, base_url or clients")
    kwargs = dict(config.provider_kwargs)
    kwargs["api_key"] = config.api_key.get_secret_value()
    if config.base_url is not None:
        kwargs["base_url"] = config.base_url
    return kwargs


def validate_provider(config: ProviderConfig) -> None:
    """Validate imports and constructor arguments without allocating any SDK clients."""
    provider_cls, model_cls = resolve_classes(config)
    try:
        inspect.signature(provider_cls).bind(**provider_arguments(config))
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    validate_settings(model_cls, {})


@asynccontextmanager
async def _sdk_context[ResourceT](
    resource: AbstractAsyncContextManager[ResourceT],
) -> AsyncGenerator[ResourceT]:
    """Protect only Provider/Model exit; never wrap graph or plugin cancel scopes."""

    async def close(
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        with anyio.fail_after(5, shield=True):
            return await resource.__aexit__(exc_type, exc_value, traceback)

    async with AsyncExitStack() as stack:
        entered = await resource.__aenter__()
        stack.push_async_exit(close)
        yield entered


def build_provider(
    provider_cls: type[Provider], config: ProviderConfig
) -> AbstractAsyncContextManager[Provider]:
    return _sdk_context(provider_cls(**provider_arguments(config)))


def build_model(
    model_cls: type[Model],
    model_name: str,
    provider: Provider,
    *,
    profile: ModelProfileSpec | None = None,
) -> AbstractAsyncContextManager[Model]:
    # Model's abstract base signature does not include the concrete protocol's
    # model_name/provider arguments. Configuration selects that constructor.
    return _sdk_context(cast(Any, model_cls)(model_name, provider=provider, profile=profile))


def validate_settings(model_cls: type[Model], data: JsonObject) -> JsonObject:
    for cls in model_cls.__mro__:
        if wrapper := _SETTINGS_INPUTS.get(cls):
            return wrapper(settings=data).model_dump(mode="json")["settings"]
    raise ValueError("No settings adapter for the configured model class")


def normalize_model_name(model_cls: type[Model], name: str) -> str:
    return name.removeprefix("models/") if issubclass(model_cls, GoogleModel) else name


def prepare_model(data: CreateModel, model_cls: type[Model]) -> ModelRow:
    """Prepare the same canonical, validated new row for manual creation and discovery."""
    normalized = CreateModel.model_validate(
        data.model_dump() | {"model_name": normalize_model_name(model_cls, data.model_name)}
    )
    return ModelRow(
        provider_id=normalized.provider_id,
        model_name=normalized.model_name,
        name=normalized.name or normalized.model_name,
        settings=validate_settings(model_cls, normalized.settings),
        context_window=normalized.context_window,
    )


async def infer_context_window(
    model_cls: type[Model], model_name: str, provider: Provider
) -> int | None:
    """Reuse SDK metadata only; never issue a model request or start a data updater."""
    async with build_model(model_cls, model_name, provider) as model:
        value = model.profile.get("context_window")
        return value if type(value) is int and value > 0 else None


async def discover_remote_models(provider: Provider) -> dict[str, str | None]:
    """Read every SDK page; defer missing display-name fallback until names are canonical."""
    client = provider.client
    if isinstance(client, AsyncOpenAI):
        page = await client.models.list()
        return {model.id: model.id async for model in page}
    if isinstance(client, Client):
        page = await client.aio.models.list()
        return {
            model.name: model.display_name or None
            async for model in page
            if model.name and "generateContent" in (model.supported_actions or [])
        }
    raise ModelDiscoveryError("No model-list adapter for the configured provider client")

"""Shared provider resources and one session model resolution boundary."""

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import httpx2
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic_core import to_jsonable_python

from kapy.agent import ModelConnection
from kapy.agent.models import ModelType
from kapy.rpc import RpcError

from .auth import Principal, Rejected, denied
from .storage import Metadata

DEFAULT_BASES = {
    "openai_responses": "https://api.openai.com/v1",
    "openai_chat": "https://api.openai.com/v1",
    "google_ai_studio": "https://generativelanguage.googleapis.com",
}


def invalid(message: str) -> Rejected:
    return Rejected(-32602, message, {"kind": "invalid_argument"})


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=256)
    type: ModelType = "openai_responses"
    base_url: str | None = None
    api_key: SecretStr | None = None

    @field_validator("base_url")
    @classmethod
    def safe_base(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or (parsed.username or parsed.password or parsed.query or parsed.fragment)
        ):
            raise ValueError("Use an HTTP(S) base URL without credentials or query")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def key_nonempty(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("API key must be nonempty")
        return value


class ModelDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    context_window_tokens: int | None = Field(None, gt=0)
    max_output_tokens: int | None = Field(None, gt=0)


class SessionModelConfig(ModelDefaults):
    model_id: UUID


def parse_session_model(value: Any) -> SessionModelConfig:
    try:
        return SessionModelConfig.model_validate_json(json.dumps(value))
    except ValidationError, ValueError, TypeError:
        raise invalid("Model accepts model_id and token budget overrides only") from None


def public_provider(row: dict) -> dict:
    return {
        **{
            key: to_jsonable_python(row[key])
            for key in ("id", "name", "type", "base_url", "revision", "created_at", "updated_at")
        },
        "has_api_key": row["api_key"] is not None,
    }


class Providers:
    def __init__(self, metadata: Metadata, http: httpx2.AsyncClient) -> None:
        self.metadata, self.http = metadata, http

    async def get(self, provider_id: UUID) -> dict:
        rows = await self.metadata.rows(
            "SELECT * FROM gateway_providers WHERE id=%s AND NOT deleted", (provider_id,)
        )
        if not rows:
            raise Rejected(
                -32004, "Provider is unavailable; select another provider", {"kind": "not_found"}
            )
        return rows[0]

    @staticmethod
    def connection(row: dict) -> ModelConnection:
        return ModelConnection(row["type"], row["base_url"], SecretStr(row["api_key"]))

    async def model(self, model_id: UUID) -> dict:
        rows = await self.metadata.rows(
            "SELECT * FROM gateway_provider_models WHERE id=%s", (model_id,)
        )
        if not rows:
            raise Rejected(-32004, "Model not found", {"kind": "not_found"})
        return rows[0]

    async def mutate(self, method: str, data: dict, canonical: dict, principal: Principal) -> dict:
        """Resource and replay receipt share one transaction; only the current row owns a key."""
        fingerprint = json.dumps(
            canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        digest = hashlib.sha256(fingerprint.encode()).hexdigest()
        request_id = data["request_id"]
        public_params = {k: v for k, v in canonical.items() if k != "api_key"}
        discovery = None
        observed_revision = None
        if method == "provider.discover":
            previous_request = await self.metadata.request(request_id)
            if previous_request is not None:
                if previous_request["principal_id"] != principal.id:
                    raise denied("Request belongs to another caller")
                if (
                    previous_request["params_hash"] != digest
                    or previous_request["method"] != method
                ):
                    raise RpcError(-32009, "Request parameters changed", {"kind": "conflict"})
                if previous_request["result"] is not None:
                    return previous_request["result"]
            provider = await self.get(data["provider_id"])
            observed_revision = provider["revision"]
            discovery = await self.discover_page(
                provider, page_token=data["page_token"], limit=data["limit"]
            )
        async with self.metadata.connection() as cursor:
            await cursor.execute(
                "INSERT INTO gateway_requests(request_id,principal_id,method,params_hash,params) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (request_id, principal.id, method, digest, Jsonb(public_params)),
            )
            await cursor.execute(
                "SELECT * FROM gateway_requests WHERE request_id=%s FOR UPDATE", (request_id,)
            )
            request = await cursor.fetchone()
            assert request is not None
            if request["principal_id"] != principal.id:
                raise denied("Request belongs to another caller")
            if request["params_hash"] != digest or request["method"] != method:
                raise RpcError(-32009, "Request parameters changed", {"kind": "conflict"})
            if request["result"] is not None:
                return request["result"]
            provider_id = data.get("provider_id") or uuid5(request_id, "provider")
            if method == "provider.model.update":
                await cursor.execute(
                    "SELECT provider_id FROM gateway_provider_models WHERE id=%s",
                    (data["model_id"],),
                )
                registered = await cursor.fetchone()
                if registered is None:
                    raise Rejected(-32004, "Model not found", {"kind": "not_found"})
                provider_id = registered["provider_id"]
            previous = None
            if method != "provider.create":
                await cursor.execute(
                    "SELECT * FROM gateway_providers WHERE id=%s AND NOT deleted FOR UPDATE",
                    (provider_id,),
                )
                previous = await cursor.fetchone()
                if previous is None:
                    raise Rejected(-32004, "Provider not found", {"kind": "not_found"})
                if (
                    method in {"provider.update", "provider.delete"}
                    and previous["revision"] != data["expected_revision"]
                ):
                    raise Rejected(-32009, "Provider revision changed", {"kind": "conflict"})
            if method == "provider.discover":
                assert previous is not None and discovery is not None
                if previous["revision"] != observed_revision:
                    raise Rejected(
                        -32009, "Provider changed during discovery", {"kind": "conflict"}
                    )
                items = []
                for observed in discovery["items"]:
                    model_id = uuid5(provider_id, observed["name"])
                    await cursor.execute(
                        "INSERT INTO "
                        "gateway_provider_models(id,provider_id,name,discovered,discovered_at) "
                        "VALUES (%s,%s,%s,%s,now()) ON CONFLICT(provider_id,name) DO UPDATE SET "
                        "discovered=EXCLUDED.discovered,discovered_at=now(),updated_at=now(),"
                        "revision=gateway_provider_models.revision+1 RETURNING *",
                        (
                            model_id,
                            provider_id,
                            observed["name"],
                            Jsonb({k: v for k, v in observed.items() if k != "name"}),
                        ),
                    )
                    row = await cursor.fetchone()
                    assert row is not None
                    items.append(to_jsonable_python(row))
                result = {"items": items, "next_page_token": discovery["next_page_token"]}
            elif method in {"provider.model.create", "provider.model.update"}:
                defaults = data["defaults"]
                if isinstance(defaults, BaseModel):
                    defaults = defaults.model_dump(mode="json")
                if method == "provider.model.create":
                    await cursor.execute(
                        "INSERT INTO gateway_provider_models(id,provider_id,name,defaults) "
                        "VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT(provider_id,name) DO NOTHING RETURNING *",
                        (
                            uuid5(provider_id, data["name"]),
                            provider_id,
                            data["name"],
                            Jsonb(defaults),
                        ),
                    )
                else:
                    await cursor.execute(
                        "UPDATE gateway_provider_models SET "
                        "defaults=%s,revision=revision+1,updated_at=now() "
                        "WHERE id=%s AND revision=%s RETURNING *",
                        (Jsonb(defaults), data["model_id"], data["expected_revision"]),
                    )
                row = await cursor.fetchone()
                if row is None:
                    raise Rejected(
                        -32009, "Model already exists or its revision changed", {"kind": "conflict"}
                    )
                result = to_jsonable_python(row)
            elif method == "provider.delete":
                await cursor.execute(
                    "UPDATE gateway_providers SET "
                    "deleted=true,api_key=NULL,revision=revision+1,updated_at=now() WHERE id=%s",
                    (provider_id,),
                )
                result = {"deleted": True}
            else:
                base = data["base_url"] or DEFAULT_BASES[data["type"]]
                key = data["api_key"]
                if key is None:
                    if previous is None:
                        raise invalid("Provider creation requires an explicit api_key")
                    if previous["type"] != data["type"] or previous["base_url"] != base:
                        raise invalid(
                            "Changing provider type or endpoint requires an explicit api_key"
                        )
                    key = SecretStr(previous["api_key"])
                if previous is None:
                    await cursor.execute(
                        "INSERT INTO gateway_providers(id,name,type,base_url,api_key) VALUES "
                        "(%s,%s,%s,%s,%s) RETURNING *",
                        (provider_id, data["name"], data["type"], base, key.get_secret_value()),
                    )
                else:
                    await cursor.execute(
                        "UPDATE gateway_providers SET "
                        "name=%s,type=%s,base_url=%s,api_key=%s,"
                        "revision=revision+1,updated_at=now() "
                        "WHERE id=%s RETURNING *",
                        (data["name"], data["type"], base, key.get_secret_value(), provider_id),
                    )
                row = await cursor.fetchone()
                assert row is not None
                if previous is not None and (
                    previous["type"] != data["type"] or previous["base_url"] != base
                ):
                    await cursor.execute(
                        "UPDATE gateway_provider_models SET "
                        "discovered='{}',discovered_at=NULL,revision=revision+1,updated_at=now() "
                        "WHERE provider_id=%s",
                        (provider_id,),
                    )
                result = public_provider(row)
            await cursor.execute(
                "UPDATE gateway_requests SET result=%s WHERE request_id=%s",
                (Jsonb(result), request_id),
            )
            return result

    async def request(
        self, connection: ModelConnection, path: str, params: dict | None = None
    ) -> dict:
        headers = (
            {"x-goog-api-key": connection.api_key.get_secret_value()}
            if connection.type == "google_ai_studio"
            else {"Authorization": "Bearer " + connection.api_key.get_secret_value()}
        )
        try:
            async with self.http.stream(
                "GET",
                connection.base_url + path,
                headers=headers,
                params=params,
                timeout=15,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise RpcError(
                        -32030, "Model discovery was rejected", {"kind": "model_discovery"}
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65_536):
                    body.extend(chunk)
                    if len(body) > 1_048_576:
                        raise invalid("Model discovery response exceeds 1 MiB")
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except httpx2.HTTPError, ValueError, TypeError:
            raise RpcError(
                -32030, "Model discovery is unavailable", {"kind": "model_discovery"}
            ) from None

    @staticmethod
    def item(raw: dict, google: bool) -> dict:
        def positive(value: Any) -> int | None:
            return value if type(value) is int and value > 0 else None

        name = raw.get("name" if google else "id")
        if not isinstance(name, str) or not name or len(name) > 256:
            raise invalid("Model discovery returned an invalid name")
        description = raw.get("description")
        return {
            "description": description[:1024] if isinstance(description, str) else None,
            "name": name.removeprefix("models/") if google else name,
            "context_window_tokens": positive(
                raw.get("inputTokenLimit" if google else "context_window_tokens")
            ),
            "max_output_tokens": positive(
                raw.get("outputTokenLimit" if google else "max_output_tokens")
            ),
        }

    async def discover_page(
        self, row: dict, *, page_token: str | None = None, limit: int = 100
    ) -> dict:
        connection = self.connection(row)
        google = connection.type == "google_ai_studio"
        query: dict = {"pageSize": limit} if google else {"limit": limit}
        if page_token:
            query["pageToken" if google else "after"] = page_token
        response = await self.request(connection, "/v1beta/models" if google else "/models", query)
        raw = response.get("models" if google else "data", [])
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise invalid("Model discovery returned an invalid list")
        items = [
            self.item(item, google)
            for item in raw
            if not google or "generateContent" in item.get("supportedGenerationMethods", [])
        ]
        next_page = response.get("nextPageToken" if google else "next_page_token")
        if google and len(raw) > limit:
            raise invalid("Model discovery exceeded its requested page size")
        if not google and "has_more" not in response and "next_page_token" not in response:
            # OpenAI's standard models endpoint returns an unpaged list. Compatible
            # servers may instead advertise a cursor; keep that protocol separate.
            items.sort(key=lambda item: item["name"])
            if page_token is not None:
                items = [item for item in items if item["name"] > page_token]
        if len(items) > limit:
            next_page = items[limit - 1]["name"]
        elif not google and response.get("has_more") and items:
            next_page = items[-1]["name"]
        if next_page is not None and (not isinstance(next_page, str) or len(next_page) > 2048):
            raise invalid("Model discovery returned an invalid page token")
        return {"items": items[:limit], "next_page_token": next_page}

    async def resolve(self, supplied: Any, inherited: dict | None = None) -> dict:
        if supplied is None:
            supplied = {}
        if not isinstance(supplied, dict):
            raise invalid("config.model must be an object")
        values = dict(inherited or {})
        if supplied.get("model_id", values.get("model_id")) != values.get("model_id"):
            values.pop("context_window_tokens", None)
            values.pop("max_output_tokens", None)
        config = parse_session_model({**values, **supplied})
        await self.effective(config)
        return config.model_dump(mode="json")

    async def effective(self, config: SessionModelConfig) -> tuple[dict, dict, int, int]:
        # One statement freezes provider and catalog from the same committed view.
        rows = await self.metadata.rows(
            "SELECT p.*,row_to_json(m) AS model FROM gateway_providers p "
            "JOIN gateway_provider_models m ON m.provider_id=p.id WHERE m.id=%s AND NOT p.deleted",
            (config.model_id,),
        )
        if not rows:
            raise Rejected(
                -32004, "Model provider is unavailable; select another model", {"kind": "not_found"}
            )
        provider = rows[0]
        model = provider.pop("model")
        defaults, observed = model["defaults"], model["discovered"]
        window = (
            config.context_window_tokens
            or defaults.get("context_window_tokens")
            or observed.get("context_window_tokens")
            or 262_144
        )
        output = (
            config.max_output_tokens
            or defaults.get("max_output_tokens")
            or observed.get("max_output_tokens")
            or 16_384
        )
        if output >= window:
            raise invalid("max_output_tokens must be smaller than context_window_tokens")
        for name, value in (("context_window_tokens", window), ("max_output_tokens", output)):
            bound = observed.get(name)
            if bound is not None and value > bound:
                raise invalid("Configured token budget exceeds the discovered provider limit")
        return provider, model, window, output

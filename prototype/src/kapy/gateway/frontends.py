"""Trusted frontend plugins access business operations through one public port."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from kapy.rpc import JsonObject, JsonValue
from kapy.settings import Settings

from .auth import Principal


class ControlAPI(Protocol):
    async def call(self, method: str, params: JsonObject, *, principal: Principal) -> JsonValue: ...


class Frontend(Protocol):
    async def run(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FrontendContext:
    settings: Settings
    control: ControlAPI
    metadata_pool: AsyncConnectionPool
    schema: str


type FrontendFactory = Callable[[FrontendContext], Frontend]

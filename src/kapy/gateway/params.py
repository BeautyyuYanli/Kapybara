"""Strict business parameters; envelope validation belongs to kapy.rpc."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue


def skill_identifier(value: str) -> str:
    return str(UUID(value))


type SkillIdentifier = Annotated[str, AfterValidator(skill_identifier)]


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionId(Params):
    session_id: UUID


class SessionConfig(Params):
    title: str = ""
    machine_ids: tuple[str, ...] = ()
    default_machine_id: str | None = None
    config: dict[str, JsonValue] = Field(default_factory=dict)


class Create(SessionConfig):
    request_id: UUID
    input: JsonValue = None
    mode: Literal["steer", "queue"] = "queue"
    waiting_id: UUID | None = None


class Update(SessionConfig):
    session_id: UUID
    request_id: UUID


class Mutation(SessionId):
    request_id: UUID


class Input(Mutation):
    payload: JsonValue
    mode: Literal["steer", "queue"] = "queue"
    waiting_id: UUID | None = None


class ListSessions(Params):
    after: UUID | None = None
    limit: int = Field(100, ge=1, le=200)


class Read(SessionId):
    after: str | None = None
    limit: int = Field(200, ge=1, le=200)


class Output(Read):
    wait_seconds: float = Field(0, ge=0, le=30)


class Wait(Mutation):
    wait_seconds: float = Field(0, ge=0, le=30)


class Search(Read):
    query: str
    mode: Literal["substring", "fulltext"] = "fulltext"


class Query(SessionId):
    sql: str
    params: dict[str, JsonValue] | None = None
    limit: int = Field(200, ge=1, le=200)


class Export(Read):
    snapshot: str | None = None


class Publish(Params):
    waiting_id: UUID
    request_id: UUID
    payload: JsonValue
    mode: Literal["steer", "queue"] = "steer"


class SkillId(Params):
    skill_id: SkillIdentifier


class SkillList(Params):
    query: str | None = None
    after_id: str | None = None
    limit: int = Field(100, ge=1, le=100)


class SkillTransfer(Mutation):
    machine_id: str | None = None
    archive_path: str = Field(min_length=1)


class SkillUpdate(SkillTransfer):
    skill_id: SkillIdentifier
    expected_revision: int = Field(ge=1)


class SkillDownload(SkillTransfer):
    skill_id: SkillIdentifier
    expected_revision: int | None = Field(None, ge=1)


class SkillDelete(SkillId):
    request_id: UUID
    expected_revision: int = Field(ge=1)

"""HTTP request and response values; no SDK handles or provider credentials escape."""

from uuid import UUID

from kapy.control.sessions import CreateSession
from kapy.control.types import DTO


class CreateSessionRequest(CreateSession):
    activity_id: UUID


class CloseSession(DTO):
    activity_id: UUID


class StartRunner(DTO):
    activity_id: UUID
    user_prompt: str


class ResumeRunner(DTO):
    activity_id: UUID

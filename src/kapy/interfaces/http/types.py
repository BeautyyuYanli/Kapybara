"""HTTP request and response values; no SDK handles or provider credentials escape."""

from uuid import UUID

from kapy.control.types import DTO


class StartRunner(DTO):
    activity_id: UUID
    user_prompt: str | None = None

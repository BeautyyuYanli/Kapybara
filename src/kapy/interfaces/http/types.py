"""HTTP request and response values; no SDK handles or provider credentials escape."""

from kapy.control.sessions import CreateSession, SubmitInput
from kapy.control.types import DTO


class CreateSessionAndSchedule(CreateSession):
    input: SubmitInput | None = None


class StartDurableRunner(DTO):
    user_prompt: str


class DurableRun(DTO):
    workflow_id: str
    run_id: str

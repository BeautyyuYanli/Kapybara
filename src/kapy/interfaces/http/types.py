"""HTTP request and response values; no SDK handles or provider credentials escape."""

from kapy.control.types import DTO


class StartRunner(DTO):
    user_prompt: str


class RunnerRun(DTO):
    activity_id: str
    run_id: str

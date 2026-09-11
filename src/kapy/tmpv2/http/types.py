"""The creation endpoint combines existing business inputs without changing create_session."""

from kapy.tmpv2.control.sessions import CreateSession, SubmitInput


class CreateSessionAndSchedule(CreateSession):
    input: SubmitInput | None = None

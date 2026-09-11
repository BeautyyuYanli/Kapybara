"""User-facing inputs, isolated by session and channel."""

from dataclasses import dataclass
from typing import Literal

from kapy.tmpv2.agent_runner.types import UserInput

type InputChannel = Literal["steer", "queued"]


@dataclass(frozen=True, slots=True)
class SessionInput:
    id: int
    content: UserInput

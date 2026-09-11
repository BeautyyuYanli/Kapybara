"""One text Agent configuration for every interface process.

Model/provider values are resolved by SessionService per start. Add shared tools
and their dependencies here when introducing them; interface code must not give
one session different execution environments merely because its input route differs.
"""

from pydantic_ai import Agent


def create_agent() -> Agent[None, str]:
    return Agent(instructions="Be concise and precise.", output_type=str)

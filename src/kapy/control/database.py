"""Control tables share isolated metadata; the application owns engines and sessions.

Tables use the connection's default PostgreSQL schema. Creating or migrating tables
is an application responsibility, never a side effect of constructing a service.
"""

from typing import ClassVar

from sqlalchemy import MetaData
from sqlmodel import SQLModel


class ControlTable(SQLModel):
    metadata: ClassVar[MetaData] = MetaData()

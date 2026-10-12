"""Immutable complete-message storage, independent of execution and live transport."""

from .repository import HistoryRepository
from .types import HistoryMessage

__all__ = ["HistoryMessage", "HistoryRepository"]

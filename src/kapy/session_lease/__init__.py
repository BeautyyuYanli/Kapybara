"""Cooperative session ownership across processes and short database transactions.

All competitors must use the same database/schema, session key and timeout policy.
A heartbeat proves renewal, not business progress. Callers decide which operations
need exclusion; SQL that bypasses this protocol is not automatically fenced.
"""

from .service import LeaseLost, SessionBusy, SessionLease, is_session_busy, open_session_lease

__all__ = ["LeaseLost", "SessionBusy", "SessionLease", "is_session_busy", "open_session_lease"]

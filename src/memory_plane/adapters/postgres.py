"""PostgreSQL adapter implementation boundary.

Track F owns this module. The SQL contract already exists in
`migrations/001_initial.sql`; implementation should use transactions, `SET LOCAL
app.tenant_id`, optimistic revisions and the outbox table.
"""

from __future__ import annotations


class PostgresMemoryLedger:
    """Production MemoryLedger placeholder with an intentionally stable name."""

    def __init__(self, dsn: str) -> None:
        """Capture configuration without opening a connection at import time."""
        self.dsn = dsn

    def connect(self) -> None:
        """Fail clearly until Track F supplies the psycopg implementation."""
        raise NotImplementedError("implement against MemoryLedger and migration 001")

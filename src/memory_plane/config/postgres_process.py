"""Secret-safe libpq subprocess configuration.

Database URLs are convenient application configuration, but passing a URL with
credentials to ``pg_dump``, ``pg_restore`` or ``psql`` exposes the password in
the process argument list.  This module removes the password from the command
line and supplies it through a mode-0600 temporary ``PGPASSFILE`` instead.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class PostgresProcessConnection:
    """Non-secret command DSN plus a private libpq environment."""

    dsn: str
    environment: dict[str, str]
    password: str | None


@contextmanager
def postgres_process_connection(
    dsn: str,
    *,
    base_environment: Mapping[str, str] | None = None,
    host: str | None = None,
    port: int | None = None,
) -> Iterator[PostgresProcessConnection]:
    """Yield a password-free DSN and a temporary libpq password file."""
    sanitized, password, pgpass_fields = _split_dsn(dsn, host=host, port=port)
    environment = dict(base_environment or os.environ)
    environment.pop("PGPASSWORD", None)
    password_file: Path | None = None
    try:
        if password is not None:
            descriptor, name = tempfile.mkstemp(prefix="obelisk-pgpass-")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(":".join(_pgpass_escape(value) for value in pgpass_fields))
                handle.write("\n")
            password_file = Path(name)
            environment["PGPASSFILE"] = str(password_file)
        yield PostgresProcessConnection(
            dsn=sanitized,
            environment=environment,
            password=password,
        )
    finally:
        if password_file is not None:
            password_file.unlink(missing_ok=True)


def password_free_postgres_dsn(
    dsn: str,
    *,
    host: str | None = None,
    port: int | None = None,
) -> tuple[str, str | None]:
    """Return a password-free URL and its decoded password for pipe transport."""
    sanitized, password, _fields = _split_dsn(dsn, host=host, port=port)
    return sanitized, password


def _split_dsn(
    dsn: str,
    *,
    host: str | None,
    port: int | None,
) -> tuple[str, str | None, tuple[str, str, str, str, str]]:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("database URL must be an absolute PostgreSQL URL")
    username = unquote(parsed.username or "")
    password = unquote(parsed.password) if parsed.password is not None else None
    target_host = host or parsed.hostname
    target_port = port or parsed.port or 5432
    display_host = f"[{target_host}]" if ":" in target_host else target_host
    user_prefix = f"{quote(username, safe='')}@" if username else ""
    port_suffix = f":{port or parsed.port}" if port is not None or parsed.port else ""
    netloc = f"{user_prefix}{display_host}{port_suffix}"
    sanitized = urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
    database = unquote(parsed.path.lstrip("/")) or "*"
    return (
        sanitized,
        password,
        (target_host, str(target_port), database, username or "*", password or ""),
    )


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")

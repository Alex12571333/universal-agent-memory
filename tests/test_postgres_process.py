from __future__ import annotations

from pathlib import Path

from memory_plane.config.postgres_process import (
    password_free_postgres_dsn,
    postgres_process_connection,
)


def test_postgres_process_connection_hides_password_in_mode_0600_file() -> None:
    secret = "pa:ss\\word"
    dsn = "postgresql://operator:pa%3Ass%5Cword@db.local:6543/memory?sslmode=require"

    with postgres_process_connection(dsn, base_environment={"PATH": "/bin"}) as connection:
        password_file = Path(connection.environment["PGPASSFILE"])

        assert connection.dsn == (
            "postgresql://operator@db.local:6543/memory?sslmode=require"
        )
        assert connection.password == secret
        assert secret not in connection.dsn
        assert "PGPASSWORD" not in connection.environment
        assert password_file.stat().st_mode & 0o777 == 0o600
        assert password_file.read_text(encoding="utf-8") == (
            "db.local:6543:memory:operator:pa\\:ss\\\\word\n"
        )

    assert not password_file.exists()


def test_password_free_postgres_dsn_supports_container_host_override() -> None:
    sanitized, password = password_free_postgres_dsn(
        "postgresql://memory_admin:s3cret@127.0.0.1:6548/memory",
        host="127.0.0.1",
        port=5432,
    )

    assert sanitized == "postgresql://memory_admin@127.0.0.1:5432/memory"
    assert password == "s3cret"

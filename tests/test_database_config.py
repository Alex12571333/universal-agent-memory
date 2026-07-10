from __future__ import annotations

from pathlib import Path

import pytest

from memory_plane.config.database import read_database_dsn


def test_database_dsn_prefers_complete_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://direct.example/memory")
    monkeypatch.setenv("UAM_DATABASE_HOST", "ignored")

    assert read_database_dsn() == "postgresql://direct.example/memory"


def test_database_dsn_assembles_components_and_reads_password_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "app-password"
    password_file.write_text("p@ss:/ word\n", encoding="utf-8")
    monkeypatch.setenv("UAM_DATABASE_HOST", "2001:db8::1")
    monkeypatch.setenv("UAM_DATABASE_PORT", "5433")
    monkeypatch.setenv("UAM_DATABASE_NAME", "memory/name")
    monkeypatch.setenv("UAM_DATABASE_USER", "app user")
    monkeypatch.setenv("UAM_DATABASE_PASSWORD_FILE", str(password_file))

    assert read_database_dsn() == (
        "postgresql://app%20user:p%40ss%3A%2F%20word@[2001:db8::1]:5433/memory%2Fname"
    )


def test_database_dsn_rejects_partial_component_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAM_DATABASE_HOST", "postgres")

    with pytest.raises(ValueError, match="missing: user, password, database"):
        read_database_dsn()


@pytest.mark.parametrize("port", ["abc", "0", "65536"])
def test_database_dsn_rejects_invalid_port(
    monkeypatch: pytest.MonkeyPatch,
    port: str,
) -> None:
    monkeypatch.setenv("UAM_DATABASE_HOST", "postgres")
    monkeypatch.setenv("UAM_DATABASE_PORT", port)
    monkeypatch.setenv("UAM_DATABASE_NAME", "memory")
    monkeypatch.setenv("UAM_DATABASE_USER", "app")
    monkeypatch.setenv("UAM_DATABASE_PASSWORD", "secret")

    with pytest.raises(ValueError, match="UAM_DATABASE_PORT"):
        read_database_dsn()

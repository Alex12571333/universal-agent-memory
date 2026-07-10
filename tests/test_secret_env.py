from __future__ import annotations

from pathlib import Path

from memory_plane.config.secrets import read_secret_env


def test_read_secret_env_prefers_direct_value(
    monkeypatch, tmp_path: Path
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("UAM_TEST_SECRET", "from-env")
    monkeypatch.setenv("UAM_TEST_SECRET_FILE", str(secret_file))

    assert read_secret_env("UAM_TEST_SECRET") == "from-env"


def test_read_secret_env_reads_file_and_strips_newline(
    monkeypatch, tmp_path: Path
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("mounted-secret\n", encoding="utf-8")
    monkeypatch.delenv("UAM_TEST_SECRET", raising=False)
    monkeypatch.setenv("UAM_TEST_SECRET_FILE", str(secret_file))

    assert read_secret_env("UAM_TEST_SECRET") == "mounted-secret"


def test_read_secret_env_uses_fallback_file(
    monkeypatch, tmp_path: Path
) -> None:
    secret_file = tmp_path / "fallback"
    secret_file.write_text("fallback-secret\n", encoding="utf-8")
    monkeypatch.delenv("UAM_PRIMARY_SECRET", raising=False)
    monkeypatch.delenv("UAM_PRIMARY_SECRET_FILE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret_file))

    assert read_secret_env("UAM_PRIMARY_SECRET", "OPENAI_API_KEY") == "fallback-secret"


def test_read_secret_env_treats_empty_file_as_unset(
    monkeypatch, tmp_path: Path
) -> None:
    secret_file = tmp_path / "empty"
    secret_file.write_text("\n", encoding="utf-8")
    monkeypatch.delenv("UAM_TEST_SECRET", raising=False)
    monkeypatch.setenv("UAM_TEST_SECRET_FILE", str(secret_file))

    assert read_secret_env("UAM_TEST_SECRET") is None

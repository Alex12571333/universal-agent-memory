from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup = _load_script("backup")
restore = _load_script("restore")


def test_backup_invokes_pg_dump(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run = Mock()
    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setenv("UAM_BACKUP_DATABASE_URL", "postgresql://example/db")
    output = tmp_path / "nested" / "uam.dump"
    monkeypatch.setattr("sys.argv", ["backup.py", str(output)])

    assert backup.main() == 0

    run.assert_called_once_with(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            f"--file={output}",
            "postgresql://example/db",
        ],
        check=True,
    )
    assert output.parent.exists()


def test_restore_invokes_pg_restore_with_optional_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = Mock()
    monkeypatch.setattr(restore.subprocess, "run", run)
    monkeypatch.setenv("UAM_RESTORE_DATABASE_URL", "postgresql://example/db")
    dump = tmp_path / "uam.dump"
    dump.write_bytes(b"PGDMP")
    monkeypatch.setattr("sys.argv", ["restore.py", str(dump), "--clean"])

    assert restore.main() == 0

    run.assert_called_once_with(
        [
            "pg_restore",
            "--no-owner",
            "--no-acl",
            "--dbname=postgresql://example/db",
            "--clean",
            "--if-exists",
            str(dump),
        ],
        check=True,
    )

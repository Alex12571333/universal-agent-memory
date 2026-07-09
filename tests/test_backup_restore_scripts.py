from __future__ import annotations

import importlib.util
import subprocess
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
restore_drill = _load_script("restore_drill")
export_vault = _load_script("export_vault")
import_vault = _load_script("import_vault")
migrate = _load_script("migrate")


def test_migration_runner_includes_every_versioned_sql_file() -> None:
    expected = {
        "001_initial.sql",
        "002_app_role.sql",
            "003_outbox_delivery.sql",
            "004_conflict_reviews.sql",
            "005_memory_status.sql",
            "006_conversation_ledger.sql",
            "007_memory_proposals.sql",
            "008_audit_events.sql",
            "009_api_key_registry.sql",
        }
    configured = {path.name for path in migrate.MIGRATIONS}

    assert configured == expected


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


def test_restore_drill_uses_temporary_docker_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backup_file = tmp_path / "obelisk.dump"
    backup_file.write_bytes(b"PGDMP")
    commands: list[list[str]] = []
    tokens = iter(("abcd1234", "passwordseed"))

    def fake_run(
        command: list[str],
        *,
        check: bool = True,
        text: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        stdout = "\n3\n0\n0\n0\n" if capture_output else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(restore_drill.subprocess, "run", fake_run)
    monkeypatch.setattr(restore_drill.secrets, "token_hex", lambda _: next(tokens))
    monkeypatch.setattr("sys.argv", ["restore_drill.py", str(backup_file)])

    assert restore_drill.main() == 0

    container = "obelisk-restore-drill-abcd1234"
    volume = f"{container}-data"
    assert commands[0] == ["docker", "volume", "create", volume]
    assert commands[1][:6] == ["docker", "run", "-d", "--name", container, "-e"]
    assert ["docker", "cp", str(backup_file), f"{container}:/tmp/obelisk-memory.dump"] in commands
    assert any(command[:4] == ["docker", "exec", container, "pg_restore"] for command in commands)
    assert any(command[:4] == ["docker", "exec", container, "psql"] for command in commands)
    assert commands[-2] == ["docker", "rm", "-f", container]
    assert commands[-1] == ["docker", "volume", "rm", "-f", volume]


def test_export_vault_builds_postgres_exporter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vault = Mock()
    vault.export.return_value = Mock(
        files=(
            Mock(path="README.md", content="# Vault\n"),
            Mock(path="semantic/fact-alpha.md", content="Alpha\n"),
        )
    )
    container = Mock(vault=vault)
    build_container = Mock(return_value=container)
    monkeypatch.setattr(export_vault, "build_postgres_container", build_container)
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setattr("sys.argv", ["export_vault.py", str(tmp_path)])

    assert export_vault.main() == 0

    build_container.assert_called_once()
    vault.export.assert_called_once()
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Vault\n"
    assert (tmp_path / "semantic" / "fact-alpha.md").read_text(encoding="utf-8") == "Alpha\n"


def test_import_vault_defaults_to_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "semantic").mkdir()
    (tmp_path / "semantic" / "mem-alpha.md").write_text("Alpha\n", encoding="utf-8")
    vault = Mock()
    vault.plan_import.return_value = Mock(
        changes=(
            Mock(
                action="unchanged",
                path="semantic/mem-alpha.md",
                message="ok",
                new_item_id=None,
            ),
        ),
        supersede_count=0,
    )
    container = Mock(vault=vault)
    build_container = Mock(return_value=container)
    monkeypatch.setattr(import_vault, "build_postgres_container", build_container)
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setattr("sys.argv", ["import_vault.py", str(tmp_path)])

    assert import_vault.main() == 0

    build_container.assert_called_once()
    vault.plan_import.assert_called_once()
    vault.apply_import.assert_not_called()


def test_import_vault_apply_uses_apply_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "mem-alpha.md").write_text("Alpha\n", encoding="utf-8")
    vault = Mock()
    vault.apply_import.return_value = Mock(
        changes=(
            Mock(
                action="supersede",
                path="core/mem-alpha.md",
                message="ok",
                new_item_id=None,
            ),
        ),
        supersede_count=1,
    )
    container = Mock(vault=vault)
    build_container = Mock(return_value=container)
    monkeypatch.setattr(import_vault, "build_postgres_container", build_container)
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setattr("sys.argv", ["import_vault.py", str(tmp_path), "--apply"])

    assert import_vault.main() == 0

    build_container.assert_called_once()
    vault.apply_import.assert_called_once()
    vault.plan_import.assert_not_called()

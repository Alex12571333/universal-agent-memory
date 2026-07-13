from __future__ import annotations

import importlib.util
import os
import subprocess
from argparse import Namespace
from pathlib import Path


def _load_drill():
    path = Path(__file__).resolve().parents[1] / "scripts" / "isolated_recovery_drill.py"
    spec = importlib.util.spec_from_file_location("isolated_recovery_drill_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_restore_container_requires_restore_drill_success_marker() -> None:
    drill = _load_drill()

    assert drill._restore_container("restore_drill=PASS container=recovery-pg-a1b2 volume=x\n") == (
        "recovery-pg-a1b2"
    )
    try:
        drill._restore_container("restore failed")
    except RuntimeError as exc:
        assert "did not report" in str(exc)
    else:
        raise AssertionError("expected missing restore marker rejection")


def test_postgres_password_is_read_without_writing_evidence(monkeypatch) -> None:
    drill = _load_drill()
    calls: list[list[str]] = []

    def fake_run(command, *, check=True, capture_output=False):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="POSTGRES_DB=memory\nPOSTGRES_PASSWORD=temporary-secret\n",
            stderr="",
        )

    monkeypatch.setattr(drill, "_run", fake_run)

    assert drill._postgres_password("recovery-pg") == "temporary-secret"
    assert calls[0][:3] == ["docker", "inspect", "--format"]


def test_probe_runs_in_restored_postgres_network_namespace(monkeypatch, tmp_path: Path) -> None:
    drill = _load_drill()
    calls: list[list[str]] = []

    def fake_run(command, *, check=True, capture_output=False):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(drill, "_run", fake_run)
    runtime_env = tmp_path / ".env"
    runtime_env.write_text("UAM_EMBEDDING_PROVIDER=fake\n", encoding="utf-8")
    args = Namespace(
        runtime_env_file=runtime_env,
        server_image="obelisk:test",
        tenant_id="00000000-0000-0000-0000-000000000001",
        workspace_id="00000000-0000-0000-0000-000000000002",
    )
    output = tmp_path / "probe.json"

    drill._run_probe(
        args,
        postgres_container="recovery-pg",
        postgres_password="secret/with space",
        collection="recovery_probe_abc",
        output=output,
        work_dir=tmp_path,
    )

    command = calls[0]
    assert ["--network", "container:recovery-pg"] == command[3:5]
    assert "UAM_QDRANT_URL=http://127.0.0.1:6333" in command
    assert (
        "UAM_DATABASE_URL=postgresql://memory_admin:secret%2Fwith%20space@127.0.0.1:5432/memory"
        in command
    )
    assert command[-2:] == ["--report", "/evidence/probe.json"]


def test_restore_drill_loads_backup_key_from_runtime_env_without_putting_it_in_argv(
    monkeypatch, tmp_path: Path
) -> None:
    drill = _load_drill()
    runtime_env = tmp_path / ".env"
    runtime_env.write_text('UAM_BACKUP_ENCRYPTION_KEY="test-backup-key"\n', encoding="utf-8")
    args = Namespace(
        backup=tmp_path / "backup.dump.enc",
        name_prefix="obelisk-isolated-recovery",
        timeout_seconds=60,
        source_docker_service="postgres",
    )
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(command, *, check=True, capture_output=False, env=None):
        calls.append((command, env))
        return subprocess.CompletedProcess(command, 0, stdout="restore_drill=PASS container=x\n")

    monkeypatch.delenv("UAM_BACKUP_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("UAM_BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.setattr(drill, "_run", fake_run)

    key = drill._backup_encryption_key(runtime_env)
    source_database_url = "postgresql://recovery-user:recovery-pass@db/recovery"
    drill._run_restore_drill(
        args,
        "suffix",
        tmp_path / "restore.json",
        backup_encryption_key=key,
        source_database_url=source_database_url,
        expected_row_counts_report=None,
    )

    command, environment = calls[0]
    assert "test-backup-key" not in " ".join(command)
    assert source_database_url not in " ".join(command)
    assert environment is not None
    assert environment["UAM_BACKUP_ENCRYPTION_KEY"] == "test-backup-key"
    assert environment["UAM_BACKUP_DATABASE_URL"] == source_database_url
    assert environment is not os.environ


def test_backup_snapshot_report_requires_adjacent_counts(tmp_path: Path) -> None:
    drill = _load_drill()
    backup = tmp_path / "obelisk-memory-20260713.dump.enc"
    backup.write_bytes(b"backup")
    report = backup.with_suffix("").with_suffix(".restore.json")
    report.write_text('{"source_row_counts":{"memory_items":1}}\n', encoding="utf-8")

    assert drill._backup_snapshot_report(backup) == report


def test_backup_encryption_key_reads_relative_secret_file(tmp_path: Path, monkeypatch) -> None:
    drill = _load_drill()
    key_file = tmp_path / "backup.key"
    key_file.write_text("file-backed-key\n", encoding="utf-8")
    runtime_env = tmp_path / ".env"
    runtime_env.write_text("UAM_BACKUP_ENCRYPTION_KEY_FILE=backup.key\n", encoding="utf-8")
    monkeypatch.delenv("UAM_BACKUP_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("UAM_BACKUP_ENCRYPTION_KEY_FILE", raising=False)

    assert drill._backup_encryption_key(runtime_env) == "file-backed-key"


def test_materialize_docker_env_file_strips_compose_quotes_and_is_private(tmp_path: Path) -> None:
    drill = _load_drill()
    source = tmp_path / ".env"
    source.write_text(
        "UAM_MEMORY_LLM_EXTRA_BODY_JSON='{" + '"mode":"compact"' + "}'\n"
        "UAM_EMBEDDING_MODEL=local-model\n",
        encoding="utf-8",
    )
    target = tmp_path / "docker.env"

    written = drill._materialize_docker_env_file(source, target)

    assert written == target
    assert target.read_text(encoding="utf-8") == (
        'UAM_MEMORY_LLM_EXTRA_BODY_JSON={"mode":"compact"}\n'
        "UAM_EMBEDDING_MODEL=local-model\n"
    )
    assert target.stat().st_mode & 0o777 == 0o600


def test_source_database_url_reads_admin_url_from_runtime_env(tmp_path: Path, monkeypatch) -> None:
    drill = _load_drill()
    runtime_env = tmp_path / ".env"
    runtime_env.write_text(
        "UAM_ADMIN_DATABASE_URL=postgresql://admin:secret@localhost/memory\n",
        encoding="utf-8",
    )
    for name in ("UAM_BACKUP_DATABASE_URL", "UAM_ADMIN_DATABASE_URL", "UAM_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)

    assert drill._source_database_url(None, runtime_env) == (
        "postgresql://admin:secret@localhost/memory"
    )


def test_failure_report_does_not_include_failure_detail(tmp_path: Path) -> None:
    drill = _load_drill()
    report = tmp_path / "failed.json"

    drill._write_failure_report(report, "CalledProcessError", "semantic-probe")

    assert report.read_text(encoding="utf-8") == (
        '{"error_type": "CalledProcessError", '
        '"format": "obelisk-isolated-semantic-recovery-drill-v1", "ok": false, '
        '"stage": "semantic-probe"}\n'
    )


def test_persist_evidence_keeps_recovery_inputs_next_to_final_report(tmp_path: Path) -> None:
    drill = _load_drill()
    temporary_restore = tmp_path / "temporary-restore.json"
    temporary_probe = tmp_path / "temporary-probe.json"
    temporary_restore.write_text('{"ok": true}\n', encoding="utf-8")
    temporary_probe.write_text('{"ok": true}\n', encoding="utf-8")
    report = tmp_path / "evidence" / "recovery.json"

    restore, probe = drill._persist_evidence(temporary_restore, temporary_probe, report)

    assert restore == report.with_name("recovery.restore-drill.json")
    assert probe == report.with_name("recovery.restored-reindex-probe.json")
    assert restore.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert probe.read_text(encoding="utf-8") == '{"ok": true}\n'

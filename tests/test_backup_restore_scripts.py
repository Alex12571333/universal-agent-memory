from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import urllib.error
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


backup = _load_script("backup")
restore = _load_script("restore")
restore_drill = _load_script("restore_drill")
check_branch_protection = _load_script("check_branch_protection")
export_audit = _load_script("export_audit")
audit_retention = _load_script("audit_retention")
scheduled_backup = _load_script("scheduled_backup")
deployment_preflight = _load_script("deployment_preflight")
export_vault = _load_script("export_vault")
import_vault = _load_script("import_vault")
migrate = _load_script("migrate")
validate_production_env = _load_script("validate_production_env")
secret_files_preflight = _load_script("secret_files_preflight")
verify_release_evidence = _load_script("verify_release_evidence")


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


def test_validate_production_env_accepts_strict_real_config(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text(
        "\n".join(
            [
                "UAM_API_KEY=ak_" + "a" * 40,
                "UAM_API_KEYS="
                "openclaw:oc_" + "b" * 32 + ":agent,"
                "hermes:hm_" + "c" * 32 + ":agent,"
                "operator:op_" + "d" * 32 + ":operator",
                "UAM_SERVER_ID=00000000-0000-0000-0000-000000000001",
                "UAM_PROJECT_ID=00000000-0000-0000-0000-000000000002",
                "UAM_PUBLIC_HOST=memory.example.com",
                "UAM_PUBLIC_EMAIL=ops@example.com",
                "POSTGRES_PASSWORD=pg_" + "e" * 40,
                "UAM_APP_DB_PASSWORD=app_" + "f" * 40,
                "MINIO_ROOT_PASSWORD=minio_" + "a" * 40,
                "UAM_CONTEXT_BUDGET_TOKENS=131072",
                "UAM_PRIVACY_ENABLED=true",
                "UAM_PRIVACY_ACTION=redact",
                "UAM_MEMORY_TEXT_ENCRYPTION=pgcrypto",
                "UAM_MEMORY_TEXT_ENCRYPTION_SCOPES=all",
                "UAM_MEMORY_TEXT_ENCRYPTION_KEY=memtext_" + "f" * 40,
                "UAM_AUDIT_SIGNING_KEY=audit_" + "b" * 40,
                "UAM_VAULT_SIGNING_KEY=vault_" + "c" * 40,
                "UAM_EMBEDDING_PROVIDER=openai",
                "UAM_EMBEDDING_MODEL=text-embedding-3-large",
                "UAM_EMBEDDING_BASE_URL=https://api.openai.com/v1",
                "UAM_EMBEDDING_API_KEY=emb_" + "g" * 40,
                "UAM_EMBEDDING_DIM=3072",
                "UAM_QDRANT_PAYLOAD_TEXT=false",
            ]
        ),
        encoding="utf-8",
    )

    values = validate_production_env.parse_env_file(env_file)
    checks = validate_production_env.validate_env(
        values,
        require_public_tls=True,
        require_signed_artifacts=True,
        require_real_embeddings=True,
    )

    assert all(check.ok for check in checks)


def test_validate_production_env_accepts_secret_files(tmp_path: Path) -> None:
    secret_values = {
        "UAM_API_KEY": "ak_" + "a" * 40,
        "UAM_API_KEYS": "openclaw:oc_" + "b" * 32 + ":agent,"
        "hermes:hm_" + "c" * 32 + ":agent,"
        "operator:op_" + "d" * 32 + ":operator",
        "POSTGRES_PASSWORD": "pg_" + "e" * 40,
        "UAM_APP_DB_PASSWORD": "app_" + "f" * 40,
        "MINIO_ROOT_PASSWORD": "minio_" + "a" * 40,
        "UAM_MEMORY_TEXT_ENCRYPTION_KEY": "memtext_" + "f" * 40,
        "UAM_AUDIT_SIGNING_KEY": "audit_" + "b" * 40,
        "UAM_VAULT_SIGNING_KEY": "vault_" + "c" * 40,
    }
    secret_lines: list[str] = []
    for key, value in secret_values.items():
        path = tmp_path / key.lower()
        path.write_text(value + "\n", encoding="utf-8")
        secret_lines.append(f"{key}_FILE={path}")

    env_file = tmp_path / ".env.production"
    env_file.write_text(
        "\n".join(
            [
                *secret_lines,
                "UAM_SERVER_ID=00000000-0000-0000-0000-000000000001",
                "UAM_PROJECT_ID=00000000-0000-0000-0000-000000000002",
                "UAM_PUBLIC_HOST=memory.example.com",
                "UAM_PUBLIC_EMAIL=ops@example.com",
                "UAM_CONTEXT_BUDGET_TOKENS=131072",
                "UAM_PRIVACY_ENABLED=true",
                "UAM_PRIVACY_ACTION=redact",
                "UAM_MEMORY_TEXT_ENCRYPTION=pgcrypto",
                "UAM_MEMORY_TEXT_ENCRYPTION_SCOPES=private,thread",
                "UAM_EMBEDDING_PROVIDER=openai",
                "UAM_EMBEDDING_MODEL=text-embedding-3-large",
                "UAM_EMBEDDING_BASE_URL=https://api.openai.com/v1",
                "UAM_EMBEDDING_DIM=3072",
                "UAM_QDRANT_PAYLOAD_TEXT=false",
            ]
        ),
        encoding="utf-8",
    )

    values = validate_production_env.parse_env_file(env_file)
    checks = validate_production_env.validate_env(
        values,
        require_public_tls=True,
        require_signed_artifacts=True,
        require_real_embeddings=True,
    )

    assert all(check.ok for check in checks)
    assert any(
        check.name == "UAM_API_KEY" and "UAM_API_KEY_FILE" in check.detail
        for check in checks
    )


def test_validate_production_env_rejects_placeholders_and_missing_public_tls() -> None:
    values = validate_production_env.parse_env_file(ROOT / ".env.production.example")

    checks = validate_production_env.validate_env(
        values,
        require_public_tls=True,
        require_signed_artifacts=True,
        require_real_embeddings=True,
    )

    failed = {check.name for check in checks if not check.ok}
    assert {
        "UAM_API_KEY",
        "UAM_API_KEYS",
        "POSTGRES_PASSWORD",
        "UAM_APP_DB_PASSWORD",
        "MINIO_ROOT_PASSWORD",
        "public-tls",
        "UAM_AUDIT_SIGNING_KEY",
        "UAM_VAULT_SIGNING_KEY",
        "UAM_MEMORY_TEXT_ENCRYPTION_KEY",
    } <= failed


def test_validate_production_env_rejects_plaintext_memory_storage() -> None:
    values = {
        "UAM_API_KEY": "ak_" + "a" * 40,
        "UAM_API_KEYS": "openclaw:oc_" + "b" * 32 + ":agent,"
        "hermes:hm_" + "c" * 32 + ":agent,"
        "operator:op_" + "d" * 32 + ":operator",
        "UAM_SERVER_ID": "00000000-0000-0000-0000-000000000001",
        "UAM_PROJECT_ID": "00000000-0000-0000-0000-000000000002",
        "POSTGRES_PASSWORD": "pg_" + "e" * 40,
        "UAM_APP_DB_PASSWORD": "app_" + "f" * 40,
        "MINIO_ROOT_PASSWORD": "minio_" + "a" * 40,
        "UAM_CONTEXT_BUDGET_TOKENS": "131072",
        "UAM_PRIVACY_ENABLED": "true",
        "UAM_PRIVACY_ACTION": "redact",
        "UAM_EMBEDDING_DIM": "3072",
        "UAM_QDRANT_PAYLOAD_TEXT": "false",
        "UAM_MEMORY_TEXT_ENCRYPTION": "off",
    }

    checks = validate_production_env.validate_env(values)

    failed = {check.name for check in checks if not check.ok}
    assert "UAM_MEMORY_TEXT_ENCRYPTION" in failed


def test_validate_production_env_rejects_unknown_memory_encryption_scope() -> None:
    values = {
        "UAM_API_KEY": "ak_" + "a" * 40,
        "UAM_API_KEYS": "openclaw:oc_" + "b" * 32 + ":agent,"
        "hermes:hm_" + "c" * 32 + ":agent,"
        "operator:op_" + "d" * 32 + ":operator",
        "UAM_SERVER_ID": "00000000-0000-0000-0000-000000000001",
        "UAM_PROJECT_ID": "00000000-0000-0000-0000-000000000002",
        "POSTGRES_PASSWORD": "pg_" + "e" * 40,
        "UAM_APP_DB_PASSWORD": "app_" + "f" * 40,
        "MINIO_ROOT_PASSWORD": "minio_" + "a" * 40,
        "UAM_CONTEXT_BUDGET_TOKENS": "131072",
        "UAM_PRIVACY_ENABLED": "true",
        "UAM_PRIVACY_ACTION": "redact",
        "UAM_MEMORY_TEXT_ENCRYPTION": "pgcrypto",
        "UAM_MEMORY_TEXT_ENCRYPTION_SCOPES": "private,nope",
        "UAM_MEMORY_TEXT_ENCRYPTION_KEY": "memtext_" + "f" * 40,
        "UAM_EMBEDDING_DIM": "3072",
        "UAM_QDRANT_PAYLOAD_TEXT": "false",
    }

    checks = validate_production_env.validate_env(values)

    failed = {check.name for check in checks if not check.ok}
    assert "UAM_MEMORY_TEXT_ENCRYPTION_SCOPES" in failed


def test_production_compose_wires_memory_text_encryption() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert (
        compose.count(
            "UAM_MEMORY_TEXT_ENCRYPTION: ${UAM_MEMORY_TEXT_ENCRYPTION:-pgcrypto}"
        )
        >= 2
    )
    assert compose.count("UAM_MEMORY_TEXT_ENCRYPTION_SCOPES: ") >= 2
    assert (
        compose.count("UAM_MEMORY_TEXT_ENCRYPTION_KEY_FILE: ")
        >= 2
    )
    assert "UAM_API_KEY_FILE: ${UAM_API_KEY_FILE:-}" in compose
    assert "UAM_API_KEYS_FILE: ${UAM_API_KEYS_FILE:-}" in compose
    assert (
        compose.count("UAM_QDRANT_PAYLOAD_TEXT: ${UAM_QDRANT_PAYLOAD_TEXT:-false}")
        >= 2
    )


def test_validate_production_env_rejects_qdrant_text_payloads(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text(
        "\n".join(
            [
                "UAM_API_KEY=ak_" + "a" * 40,
                "UAM_API_KEYS="
                "openclaw:oc_" + "b" * 32 + ":agent,"
                "hermes:hm_" + "c" * 32 + ":agent,"
                "operator:op_" + "d" * 32 + ":operator",
                "UAM_SERVER_ID=00000000-0000-0000-0000-000000000001",
                "UAM_PROJECT_ID=00000000-0000-0000-0000-000000000002",
                "POSTGRES_PASSWORD=pg_" + "e" * 40,
                "UAM_APP_DB_PASSWORD=app_" + "f" * 40,
                "MINIO_ROOT_PASSWORD=minio_" + "a" * 40,
                "UAM_CONTEXT_BUDGET_TOKENS=131072",
                "UAM_PRIVACY_ENABLED=true",
                "UAM_PRIVACY_ACTION=redact",
                "UAM_EMBEDDING_PROVIDER=openai",
                "UAM_EMBEDDING_MODEL=text-embedding-3-large",
                "UAM_EMBEDDING_BASE_URL=https://api.openai.com/v1",
                "UAM_EMBEDDING_API_KEY=emb_" + "g" * 40,
                "UAM_EMBEDDING_DIM=3072",
                "UAM_QDRANT_PAYLOAD_TEXT=true",
            ]
        ),
        encoding="utf-8",
    )

    values = validate_production_env.parse_env_file(env_file)
    checks = validate_production_env.validate_env(values)

    failed = {check.name for check in checks if not check.ok}
    assert "UAM_QDRANT_PAYLOAD_TEXT" in failed


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


def test_export_audit_writes_jsonl_manifest_and_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tenant = uuid4()
    workspace = uuid4()
    event = export_audit.AuditEvent(
        tenant_id=tenant,
        workspace_id=workspace,
        action="memory.retain",
        actor="operator",
        actor_type="operator",
        resource_type="memory_item",
        resource_id="mem-alpha",
        metadata={"path": "semantic/mem-alpha.md"},
        created_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
    )
    ledger = Mock()
    audit = Mock()
    audit.list_events.return_value = (event,)
    monkeypatch.setattr(export_audit, "PostgresMemoryLedger", Mock(return_value=ledger))
    monkeypatch.setattr(export_audit, "AuditLogService", Mock(return_value=audit))
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_audit.py",
            str(tmp_path),
            "--tenant-id",
            str(tenant),
            "--workspace-id",
            str(workspace),
            "--action",
            "memory.retain",
            "--limit",
            "25",
        ],
    )

    assert export_audit.main() == 0

    events = (tmp_path / "audit-events.jsonl").read_text(encoding="utf-8").splitlines()
    manifest_bytes = (tmp_path / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    events_digest = hashlib.sha256((tmp_path / "audit-events.jsonl").read_bytes()).hexdigest()
    checksum = (tmp_path / "manifest.sha256").read_text(encoding="utf-8")

    ledger.connect.assert_called_once()
    audit.list_events.assert_called_once_with(
        tenant,
        workspace_id=workspace,
        action="memory.retain",
        resource_type=None,
        created_after=None,
        created_before=None,
        limit=25,
    )
    assert len(events) == 1
    assert json.loads(events[0])["metadata"]["path"] == "semantic/mem-alpha.md"
    assert manifest["format"] == "obelisk-audit-export-v1"
    assert manifest["event_count"] == 1
    assert manifest["filters"]["tenant_id"] == str(tenant)
    assert manifest["filters"]["workspace_id"] == str(workspace)
    assert manifest["files"][0]["sha256"] == events_digest
    assert checksum == f"{manifest_digest}  manifest.json\n"


def test_export_audit_can_export_all_pages_with_time_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tenant = uuid4()
    event_new = export_audit.AuditEvent(
        tenant_id=tenant,
        workspace_id=None,
        action="memory.retain",
        actor="operator",
        actor_type="operator",
        resource_type="memory_item",
        created_at=datetime(2026, 7, 10, 12, 2, tzinfo=UTC),
    )
    event_mid = export_audit.AuditEvent(
        tenant_id=tenant,
        workspace_id=None,
        action="memory.retain",
        actor="operator",
        actor_type="operator",
        resource_type="memory_item",
        created_at=datetime(2026, 7, 10, 12, 1, tzinfo=UTC),
    )
    event_old = export_audit.AuditEvent(
        tenant_id=tenant,
        workspace_id=None,
        action="memory.retain",
        actor="operator",
        actor_type="operator",
        resource_type="memory_item",
        created_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
    )
    ledger = Mock()
    audit = Mock()
    audit.list_events.side_effect = ((event_new, event_mid), (event_old,))
    monkeypatch.setattr(export_audit, "PostgresMemoryLedger", Mock(return_value=ledger))
    monkeypatch.setattr(export_audit, "AuditLogService", Mock(return_value=audit))
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_audit.py",
            str(tmp_path),
            "--tenant-id",
            str(tenant),
            "--all-workspaces",
            "--all-pages",
            "--batch-size",
            "2",
            "--since",
            "2026-07-10T12:00:00Z",
            "--until",
            "2026-07-10T12:03:00Z",
        ],
    )

    assert export_audit.main() == 0

    lines = (tmp_path / "audit-events.jsonl").read_text(encoding="utf-8").splitlines()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    first_call = audit.list_events.call_args_list[0].kwargs
    second_call = audit.list_events.call_args_list[1].kwargs
    assert len(lines) == 3
    assert manifest["event_count"] == 3
    assert manifest["filters"]["all_pages"] is True
    assert manifest["filters"]["page_count"] == 2
    assert first_call["limit"] == 2
    assert second_call["created_before"] == event_mid.created_at
    assert second_call["before_event_id"] == event_mid.id


def test_export_audit_signs_and_verifies_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant = uuid4()
    event = export_audit.AuditEvent(
        tenant_id=tenant,
        workspace_id=None,
        action="settings.models.update",
        actor="operator",
        actor_type="operator",
        resource_type="model_settings",
        metadata={"provider": "tei"},
        created_at=datetime(2026, 7, 10, 12, 30, tzinfo=UTC),
    )
    ledger = Mock()
    audit = Mock()
    audit.list_events.return_value = (event,)
    monkeypatch.setattr(export_audit, "PostgresMemoryLedger", Mock(return_value=ledger))
    monkeypatch.setattr(export_audit, "AuditLogService", Mock(return_value=audit))
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_audit.py",
            str(tmp_path),
            "--tenant-id",
            str(tenant),
            "--all-workspaces",
            "--signing-key",
            "secret-signing-key",
        ],
    )

    assert export_audit.main() == 0
    capsys.readouterr()

    manifest_bytes = (tmp_path / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    expected_signature = export_audit._hmac_sha256("secret-signing-key", manifest_bytes)
    signature = (tmp_path / "manifest.sig").read_text(encoding="utf-8")
    assert manifest["signature_algorithm"] == "hmac-sha256"
    assert signature == f"{expected_signature}  manifest.json\n"

    monkeypatch.setattr(
        "sys.argv",
        [
            "export_audit.py",
            str(tmp_path),
            "--verify",
            "--signing-key",
            "secret-signing-key",
        ],
    )
    assert export_audit.main() == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True

    monkeypatch.setattr(
        "sys.argv",
        ["export_audit.py", str(tmp_path), "--verify", "--signing-key", "wrong-key"],
    )
    assert export_audit.main() == 1
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["ok"] is False
    assert any(check["name"] == "manifest.sig" for check in rejected["checks"])


def test_audit_retention_dry_run_exports_and_verifies_without_pruning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tenant = uuid4()
    workspace = uuid4()
    commands: list[list[str]] = []
    audit = Mock()

    def fake_run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        bundle = Path(command[2])
        if "--verify" not in command:
            bundle.mkdir(parents=True, exist_ok=True)
            (bundle / "manifest.json").write_text(
                json.dumps({"event_count": 3}),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(audit_retention.subprocess, "run", fake_run)
    monkeypatch.setattr(audit_retention, "PostgresMemoryLedger", Mock())
    monkeypatch.setattr(audit_retention, "AuditLogService", Mock(return_value=audit))
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_retention.py",
            "--database-url",
            "postgresql://example/db",
            "--tenant-id",
            str(tenant),
            "--workspace-id",
            str(workspace),
            "--cutoff",
            "2026-07-01T00:00:00Z",
            "--export-root",
            str(tmp_path),
            "--signing-key",
            "audit-signing-key",
        ],
    )

    assert audit_retention.main() == 0

    assert len(commands) == 2
    assert "--all-pages" in commands[0]
    assert "--until" in commands[0]
    assert "--verify" in commands[1]
    audit.prune_events.assert_not_called()


def test_audit_retention_apply_requires_signed_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_retention.py",
            "--database-url",
            "postgresql://example/db",
            "--cutoff",
            "2026-07-01T00:00:00Z",
            "--export-root",
            str(tmp_path),
            "--apply",
        ],
    )

    with pytest.raises(SystemExit):
        audit_retention.main()


def test_audit_retention_apply_prunes_only_after_verify(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tenant = uuid4()
    commands: list[list[str]] = []
    ledger = Mock()
    audit = Mock()
    audit.prune_events.side_effect = [2, 0]

    def fake_run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        bundle = Path(command[2])
        if "--verify" not in command:
            bundle.mkdir(parents=True, exist_ok=True)
            (bundle / "manifest.json").write_text(
                json.dumps({"event_count": 2}),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(audit_retention.subprocess, "run", fake_run)
    monkeypatch.setattr(audit_retention, "PostgresMemoryLedger", Mock(return_value=ledger))
    monkeypatch.setattr(audit_retention, "AuditLogService", Mock(return_value=audit))
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_retention.py",
            "--database-url",
            "postgresql://example/db",
            "--tenant-id",
            str(tenant),
            "--all-workspaces",
            "--cutoff",
            "2026-07-01T00:00:00Z",
            "--export-root",
            str(tmp_path / "exports"),
            "--signing-key",
            "audit-signing-key",
            "--apply",
            "--batch-size",
            "2",
            "--json-report",
            str(report),
        ],
    )

    assert audit_retention.main() == 0

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["verified_export"] is True
    assert payload["signed_export"] is True
    assert payload["pruned_count"] == 2
    assert "--verify" in commands[1]
    ledger.connect.assert_called_once()
    audit.prune_events.assert_any_call(
        tenant,
        workspace_id=None,
        created_before=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        limit=2,
    )


def test_audit_retention_does_not_prune_when_verify_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit = Mock()

    def fake_run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        bundle = Path(command[2])
        if "--verify" not in command:
            bundle.mkdir(parents=True, exist_ok=True)
            (bundle / "manifest.json").write_text(
                json.dumps({"event_count": 2}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(audit_retention.subprocess, "run", fake_run)
    monkeypatch.setattr(audit_retention, "PostgresMemoryLedger", Mock())
    monkeypatch.setattr(audit_retention, "AuditLogService", Mock(return_value=audit))
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_retention.py",
            "--database-url",
            "postgresql://example/db",
            "--cutoff",
            "2026-07-01T00:00:00Z",
            "--export-root",
            str(tmp_path),
            "--signing-key",
            "audit-signing-key",
            "--apply",
        ],
    )

    assert audit_retention.main() == 1
    audit.prune_events.assert_not_called()


def test_check_branch_protection_accepts_pr_checks_and_admin_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "required_status_checks": {
            "strict": True,
            "contexts": ["python", "web"],
        },
        "enforce_admins": {"enabled": True},
    }
    monkeypatch.setattr(
        check_branch_protection.urllib.request,
        "urlopen",
        _fake_urlopen(payload),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_branch_protection.py",
            "--repo",
            "Alex12571333/universal-agent-memory",
            "--token",
            "ghp_test",
        ],
    )

    assert check_branch_protection.main() == 0


def test_check_branch_protection_rejects_missing_required_status_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "required_status_checks": {
            "strict": True,
            "contexts": ["python"],
        },
        "enforce_admins": {"enabled": True},
    }
    monkeypatch.setattr(
        check_branch_protection.urllib.request,
        "urlopen",
        _fake_urlopen(payload),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_branch_protection.py",
            "--repo",
            "Alex12571333/universal-agent-memory",
            "--token",
            "ghp_test",
        ],
    )

    assert check_branch_protection.main() == 1


def test_scheduled_backup_runs_backup_drill_audit_and_writes_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        check: bool = False,
        text: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    report = tmp_path / "report.json"
    monkeypatch.setattr(scheduled_backup.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "scheduled_backup.py",
            "--backup-dir",
            str(tmp_path / "backups"),
            "--audit-dir",
            str(tmp_path / "audit"),
            "--report",
            str(report),
            "--database-url",
            "postgresql://example/db",
            "--timestamp",
            "20260710T120000Z",
        ],
    )

    assert scheduled_backup.main() == 0

    payload = json.loads(report.read_text(encoding="utf-8"))
    names = [step["name"] for step in payload["steps"]]
    assert payload["ok"] is True
    assert payload["backup_path"].endswith("obelisk-memory-20260710T120000Z.dump")
    assert names == ["backup", "restore_drill", "audit_export"]
    assert "backup.py" in commands[0][1]
    assert "restore_drill.py" in commands[1][1]
    assert "export_audit.py" in commands[2][1]


def test_scheduled_backup_alerts_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    alerts: list[dict[str, object]] = []

    def fake_run(
        command: list[str],
        *,
        check: bool = False,
        text: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="boom")

    def fake_send_alert(_webhook: str, report: dict[str, object]) -> None:
        alerts.append(report)

    report = tmp_path / "report.json"
    monkeypatch.setattr(scheduled_backup.subprocess, "run", fake_run)
    monkeypatch.setattr(scheduled_backup, "_send_alert", fake_send_alert)
    monkeypatch.setattr(
        "sys.argv",
        [
            "scheduled_backup.py",
            "--backup-dir",
            str(tmp_path / "backups"),
            "--report",
            str(report),
            "--database-url",
            "postgresql://example/db",
            "--alert-webhook",
            "https://alerts.example/backup",
            "--skip-audit-export",
            "--timestamp",
            "20260710T120000Z",
        ],
    )

    assert scheduled_backup.main() == 1

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["steps"][0]["name"] == "backup"
    assert payload["steps"][0]["returncode"] == 7
    assert alerts and alerts[0]["ok"] is False


def test_verify_release_evidence_accepts_complete_manifest(tmp_path: Path) -> None:
    manifest = _write_release_evidence_bundle(tmp_path)

    checks = verify_release_evidence.verify_manifest(manifest)

    assert all(check.passed for check in checks)
    assert {check.name for check in checks} >= {
        "agent_soak:openclaw",
        "agent_soak:hermes",
        "load_smoke:parallelism",
        "scheduled_backup:restore-drill",
        "audit_retention:verified-export",
        "deployment_preflight:backend-not-public",
        "secret_files:all-required-secrets-checked",
        "vault_import:verified-signed-manifest",
        "branch_protection:passed",
        "ui_walkthrough:model-probe-not-skipped",
    }


def test_verify_release_evidence_rejects_skipped_restore_drill(tmp_path: Path) -> None:
    manifest = _write_release_evidence_bundle(tmp_path)
    backup_path = tmp_path / "scheduled-backup.json"
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    for step in backup["steps"]:
        if step["name"] == "restore_drill":
            step["skipped"] = True
    backup_path.write_text(json.dumps(backup), encoding="utf-8")

    checks = verify_release_evidence.verify_manifest(manifest)

    restore_check = next(
        check for check in checks if check.name == "scheduled_backup:restore-drill"
    )
    assert restore_check.passed is False
    assert not all(check.passed for check in checks)


def test_verify_release_evidence_rejects_unsigned_vault_import(tmp_path: Path) -> None:
    manifest = _write_release_evidence_bundle(tmp_path)
    vault_import_path = tmp_path / "vault-import.json"
    payload = json.loads(vault_import_path.read_text(encoding="utf-8"))
    payload["require_signature"] = False
    payload["manifest_signed"] = False
    vault_import_path.write_text(json.dumps(payload), encoding="utf-8")

    checks = verify_release_evidence.verify_manifest(manifest)

    signature_check = next(
        check for check in checks if check.name == "vault_import:require-signature"
    )
    signed_check = next(
        check for check in checks if check.name == "vault_import:verified-signed-manifest"
    )
    assert signature_check.passed is False
    assert signed_check.passed is False
    assert not all(check.passed for check in checks)


def test_verify_release_evidence_rejects_reachable_backend(tmp_path: Path) -> None:
    manifest = _write_release_evidence_bundle(tmp_path)
    preflight_path = tmp_path / "deployment-preflight.json"
    payload = json.loads(preflight_path.read_text(encoding="utf-8"))
    payload["ok"] = False
    payload["backend_publicly_reachable"] = True
    for check in payload["checks"]:
        if check["name"] == "backend-not-public":
            check["ok"] = False
            check["detail"] = "direct backend reachable with status=200"
    preflight_path.write_text(json.dumps(payload), encoding="utf-8")

    checks = verify_release_evidence.verify_manifest(manifest)

    backend_check = next(
        check for check in checks if check.name == "deployment_preflight:backend-not-public"
    )
    assert backend_check.passed is False
    assert not all(check.passed for check in checks)


def test_verify_release_evidence_rejects_raw_secret_env(tmp_path: Path) -> None:
    manifest = _write_release_evidence_bundle(tmp_path)
    secret_files_path = tmp_path / "secret-files.json"
    payload = json.loads(secret_files_path.read_text(encoding="utf-8"))
    payload["ok"] = False
    for check in payload["checks"]:
        if check["name"] == "UAM_API_KEY:raw-empty":
            check["ok"] = False
            check["detail"] = "raw secret env is set"
    secret_files_path.write_text(json.dumps(payload), encoding="utf-8")

    checks = verify_release_evidence.verify_manifest(manifest)

    ok_check = next(check for check in checks if check.name == "secret_files:ok")
    assert ok_check.passed is False
    assert not all(check.passed for check in checks)


def test_verify_release_evidence_json_cli_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _write_release_evidence_bundle(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["verify_release_evidence.py", str(manifest), "--json"],
    )

    assert verify_release_evidence.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert any(check["name"] == "branch_protection:passed" for check in payload["checks"])


def _write_release_evidence_bundle(tmp_path: Path) -> Path:
    _write_json(
        tmp_path / "agent-soak.json",
        {
            "format": "obelisk-agent-soak-v1",
            "ok": True,
            "checks": [
                {"name": "health", "ok": True},
                {"name": "openclaw:recall:0", "ok": True},
                {"name": "hermes:recall:0", "ok": True},
                {"name": "cross-workspace-leakage", "ok": True},
            ],
        },
    )
    _write_json(
        tmp_path / "memory-llm.json",
        {
            "format": "obelisk-memory-llm-eval-v1",
            "ok": True,
            "checks": [
                {"name": "chat-completions", "ok": True},
                {"name": "json-memory-curation", "ok": True},
            ],
        },
    )
    _write_json(
        tmp_path / "metrics-health.json",
        {
            "format": "obelisk-metrics-health-v1",
            "ok": True,
            "checks": [
                {"name": "outbox_pending_total", "ok": True},
                {"name": "outbox_dead_letter_total", "ok": True},
                {"name": "outbox_lag_seconds", "ok": True},
                {"name": "processed_events_inflight_total", "ok": True},
            ],
        },
    )
    _write_json(
        tmp_path / "load-smoke.json",
        {
            "format": "obelisk-load-smoke-v1",
            "ok": True,
            "agents": 4,
            "total_operations": 20,
            "checks": [
                {"name": "health", "ok": True},
                {"name": "concurrent-retain-recall", "ok": True},
                {"name": "error-rate", "ok": True},
                {"name": "retain-p95", "ok": True},
                {"name": "recall-p95", "ok": True},
                {"name": "metrics-backlog", "ok": True},
            ],
        },
    )
    _write_json(
        tmp_path / "scheduled-backup.json",
        {
            "format": "obelisk-scheduled-backup-report-v1",
            "ok": True,
            "steps": [
                {"name": "backup", "ok": True},
                {"name": "restore_drill", "ok": True},
                {"name": "audit_export", "ok": True},
            ],
        },
    )
    _write_json(
        tmp_path / "audit-retention.json",
        {
            "format": "obelisk-audit-retention-v1",
            "ok": True,
            "dry_run": False,
            "verified_export": True,
            "signed_export": True,
            "pruned_count": 12,
        },
    )
    _write_json(
        tmp_path / "deployment-preflight.json",
        {
            "format": "obelisk-deployment-preflight-v1",
            "ok": True,
            "public_url": "https://memory.example.com/",
            "backend_url": "http://memory.example.com:6798/",
            "backend_probe_performed": True,
            "backend_publicly_reachable": False,
            "checks": [
                {"name": "public-url-https", "ok": True},
                {"name": "public-health", "ok": True},
                {"name": "public-security-headers", "ok": True},
                {"name": "backend-not-public", "ok": True},
            ],
        },
    )
    _write_json(
        tmp_path / "secret-files.json",
        {
            "format": "obelisk-secret-files-preflight-v1",
            "ok": True,
            "required_secrets": ["UAM_API_KEY", "UAM_API_KEYS"],
            "allowed_prefixes": [str(tmp_path / "secrets")],
            "checks": [
                {"name": "UAM_API_KEY:raw-empty", "ok": True},
                {"name": "UAM_API_KEY:file-configured", "ok": True},
                {"name": "UAM_API_KEY:file-readable", "ok": True},
                {"name": "UAM_API_KEY:file-prefix", "ok": True},
                {"name": "UAM_API_KEYS:raw-empty", "ok": True},
                {"name": "UAM_API_KEYS:file-configured", "ok": True},
                {"name": "UAM_API_KEYS:file-readable", "ok": True},
                {"name": "UAM_API_KEYS:file-prefix", "ok": True},
            ],
        },
    )
    _write_json(
        tmp_path / "vault-import.json",
        {
            "format": "obelisk-vault-import-report-v1",
            "ok": True,
            "mode": "planned",
            "require_manifest": False,
            "require_signature": True,
            "manifest_verified": True,
            "manifest_signed": True,
            "manifest_file_count": 1,
            "change_count": 1,
            "supersede_count": 0,
            "actions": {"unchanged": 1},
        },
    )
    _write_json(
        tmp_path / "branch-protection.json",
        {
            "passed": True,
            "checks": [
                {"name": "pull-request-required", "passed": True},
                {"name": "status-checks-required", "passed": True},
                {"name": "strict-status-checks", "passed": True},
                {"name": "admins-enforced", "passed": True},
            ],
        },
    )
    _write_json(
        tmp_path / "ui-walkthrough.json",
        {
            "format": "obelisk-ui-walkthrough-v1",
            "ok": True,
            "checks": [
                {"name": "ui-served", "ok": True, "detail": "fallback UI served"},
                {"name": "retain-recall", "ok": True, "detail": "marker recalled"},
                {"name": "conflict-decision", "ok": True, "detail": "decision persisted"},
                {
                    "name": "vault-editable-text",
                    "ok": True,
                    "detail": "ordinary memory text only",
                },
                {"name": "vault-archive", "ok": True, "detail": "archived"},
                {"name": "model-settings-probe", "ok": True, "detail": "probe ran"},
                {"name": "reindex", "ok": True, "detail": "reindexed"},
                {"name": "metrics-surface", "ok": True, "detail": "metrics exposed"},
            ],
        },
    )
    manifest = tmp_path / "release-evidence.json"
    _write_json(
        manifest,
        {
            "format": "obelisk-release-evidence-manifest-v1",
            "release": "test",
            "artifacts": {
                "agent_soak": "agent-soak.json",
                "memory_llm": "memory-llm.json",
                "load_smoke": "load-smoke.json",
                "metrics_health": "metrics-health.json",
                "scheduled_backup": "scheduled-backup.json",
                "audit_retention": "audit-retention.json",
                "deployment_preflight": "deployment-preflight.json",
                "secret_files": "secret-files.json",
                "vault_import": "vault-import.json",
                "branch_protection": "branch-protection.json",
                "ui_walkthrough": "ui-walkthrough.json",
            },
        },
    )
    return manifest


def test_secret_files_preflight_accepts_file_backed_secrets(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "run" / "secrets"
    secrets_dir.mkdir(parents=True)
    env_lines = []
    required = ("UAM_API_KEY", "UAM_API_KEYS")
    for name in required:
        secret_path = secrets_dir / name.lower()
        secret_path.write_text(f"{name.lower()}_value\n", encoding="utf-8")
        env_lines.append(f"{name}=")
        env_lines.append(f"{name}_FILE={secret_path}")
    env_file = tmp_path / ".env.production"
    env_file.write_text("\n".join(env_lines), encoding="utf-8")

    report = secret_files_preflight.run_preflight(
        env_file=env_file,
        required_secrets=required,
        allowed_prefixes=(str(secrets_dir),),
    )

    assert report["ok"] is True


def test_secret_files_preflight_rejects_raw_secret_values(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "run" / "secrets"
    secrets_dir.mkdir(parents=True)
    secret_path = secrets_dir / "uam_api_key"
    secret_path.write_text("file-value\n", encoding="utf-8")
    env_file = tmp_path / ".env.production"
    env_file.write_text(
        f"UAM_API_KEY=raw-value\nUAM_API_KEY_FILE={secret_path}\n",
        encoding="utf-8",
    )

    report = secret_files_preflight.run_preflight(
        env_file=env_file,
        required_secrets=("UAM_API_KEY",),
        allowed_prefixes=(str(secrets_dir),),
    )

    assert report["ok"] is False
    assert any(
        check["name"] == "UAM_API_KEY:raw-empty" and check["ok"] is False
        for check in report["checks"]
    )


def test_deployment_preflight_passes_when_public_https_and_backend_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: object, timeout: float = 0) -> object:
        assert isinstance(request, deployment_preflight.urllib.request.Request)
        url = str(request.full_url)
        if url == "https://memory.example.com/health":
            return _FakeHttpResponse(
                status=200,
                headers={
                    "Strict-Transport-Security": "max-age=31536000",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                },
            )
        raise urllib.error.URLError("blocked")

    monkeypatch.setattr(deployment_preflight.urllib.request, "urlopen", fake_urlopen)

    report = deployment_preflight.run_preflight(
        public_url="https://memory.example.com",
        backend_url="http://memory.example.com:6798",
        api_key="secret",
    )

    assert report["ok"] is True
    assert report["backend_probe_performed"] is True
    assert report["backend_publicly_reachable"] is False


def test_deployment_preflight_fails_when_backend_is_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: object, timeout: float = 0) -> object:
        return _FakeHttpResponse(
            status=200,
            headers={
                "Strict-Transport-Security": "max-age=31536000",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )

    monkeypatch.setattr(deployment_preflight.urllib.request, "urlopen", fake_urlopen)

    report = deployment_preflight.run_preflight(
        public_url="https://memory.example.com",
        backend_url="http://memory.example.com:6798",
        api_key="secret",
    )

    assert report["ok"] is False
    assert report["backend_publicly_reachable"] is True


class _FakeHttpResponse:
    def __init__(self, *, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
    assert (tmp_path / ".uam-vault-manifest.json").exists()
    assert (tmp_path / ".uam-vault-manifest.sha256").exists()


def test_export_vault_can_sign_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vault = Mock()
    vault.export.return_value = Mock(
        files=(Mock(path="semantic/mem-alpha.md", content="Alpha\n"),)
    )
    container = Mock(vault=vault)
    monkeypatch.setattr(export_vault, "build_postgres_container", Mock(return_value=container))
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setattr(
        "sys.argv",
        ["export_vault.py", str(tmp_path), "--signing-key", "vault-secret"],
    )

    assert export_vault.main() == 0

    signature = (tmp_path / ".uam-vault-manifest.sig").read_text(encoding="utf-8")
    assert signature.startswith("hmac-sha256:")


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


def test_import_vault_verifies_signed_manifest_before_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "mem-alpha.md").write_text("Alpha\n", encoding="utf-8")
    export_vault.write_vault_manifest(
        tmp_path,
        tenant_id="00000000-0000-0000-0000-000000000001",
        workspace_id="00000000-0000-0000-0000-000000000002",
        signing_key="vault-secret",
    )
    vault = Mock()
    vault.apply_import.return_value = Mock(
        changes=(
            Mock(
                action="unchanged",
                path="core/mem-alpha.md",
                message="ok",
                new_item_id=None,
            ),
        ),
        supersede_count=0,
    )
    container = Mock(vault=vault)
    monkeypatch.setattr(import_vault, "build_postgres_container", Mock(return_value=container))
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setattr(
        "sys.argv",
        [
            "import_vault.py",
            str(tmp_path),
            "--apply",
            "--require-signature",
            "--signing-key",
            "vault-secret",
            "--json-report",
            str(tmp_path / "ops" / "vault-import.json"),
        ],
    )

    assert import_vault.main() == 0

    vault.apply_import.assert_called_once()
    report = json.loads((tmp_path / "ops" / "vault-import.json").read_text(encoding="utf-8"))
    assert report["format"] == "obelisk-vault-import-report-v1"
    assert report["ok"] is True
    assert report["mode"] == "applied"
    assert report["require_signature"] is True
    assert report["manifest_verified"] is True
    assert report["manifest_signed"] is True
    assert report["manifest_file_count"] == 1


def test_import_vault_rejects_tampered_signed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "core").mkdir()
    note = tmp_path / "core" / "mem-alpha.md"
    note.write_text("Alpha\n", encoding="utf-8")
    export_vault.write_vault_manifest(
        tmp_path,
        tenant_id="00000000-0000-0000-0000-000000000001",
        workspace_id="00000000-0000-0000-0000-000000000002",
        signing_key="vault-secret",
    )
    note.write_text("Tampered\n", encoding="utf-8")
    vault = Mock()
    container = Mock(vault=vault)
    monkeypatch.setattr(import_vault, "build_postgres_container", Mock(return_value=container))
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setattr(
        "sys.argv",
        [
            "import_vault.py",
            str(tmp_path),
            "--apply",
            "--require-signature",
            "--signing-key",
            "vault-secret",
        ],
    )

    with pytest.raises(ValueError, match="mismatch"):
        import_vault.main()

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


def _fake_urlopen(payload: dict[str, object]):
    @contextmanager
    def opener(*_args: object, **_kwargs: object):
        response = Mock()
        response.read.return_value = json.dumps(payload).encode("utf-8")
        yield response

    return opener

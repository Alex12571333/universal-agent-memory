from __future__ import annotations

import importlib.util
import plistlib
from pathlib import Path


def _load_installer():
    path = Path(__file__).resolve().parents[1] / "scripts" / "install_launchd_ops.py"
    spec = importlib.util.spec_from_file_location("launchd_ops_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_adds_weekly_isolated_semantic_recovery_job(tmp_path: Path) -> None:
    installer = _load_installer()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / "ops.env"
    env_file.write_text("OBELISK_PYTHON=/usr/bin/python3\n", encoding="utf-8")
    launch_agents = tmp_path / "LaunchAgents"

    generated = installer.install(
        workspace=workspace,
        env_file=env_file,
        launch_agents=launch_agents,
    )

    recovery_plist = launch_agents / "com.obelisk-memory.semantic-recovery.plist"
    assert recovery_plist in generated
    with recovery_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["StartCalendarInterval"] == {"Hour": 5, "Minute": 13, "Weekday": 0}
    wrapper = launch_agents / "obelisk-memory/semantic-recovery.zsh"
    text = wrapper.read_text(encoding="utf-8")
    assert "isolated_recovery_drill.py" in text
    assert "OBELISK_RUNTIME_ENV_FILE" in text
    assert "${OBELISK_RUNTIME_ENV_FILE:-$PWD/.env}" in text
    assert "latest_backup" in text


def test_installer_enforces_signed_backup_and_audit_retention(tmp_path: Path) -> None:
    installer = _load_installer()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / "ops.env"
    env_file.write_text("OBELISK_PYTHON=/usr/bin/python3\n", encoding="utf-8")
    launch_agents = tmp_path / "LaunchAgents"

    generated = installer.install(
        workspace=workspace,
        env_file=env_file,
        launch_agents=launch_agents,
    )

    names = {path.name for path in generated}
    assert "com.obelisk-memory.audit-retention.plist" in names
    assert "com.obelisk-memory.runtime-dependencies.plist" in names
    backup = (launch_agents / "obelisk-memory/backup.zsh").read_text(encoding="utf-8")
    audit = (launch_agents / "obelisk-memory/audit-retention.zsh").read_text(
        encoding="utf-8"
    )
    maintenance = (launch_agents / "obelisk-memory/maintenance.zsh").read_text(
        encoding="utf-8"
    )
    runtime = (launch_agents / "obelisk-memory/runtime-dependencies.zsh").read_text(
        encoding="utf-8"
    )

    assert "scheduled_backup.py" in backup and "--require-signature" in backup
    assert "audit_retention.py" in audit and "--apply" in audit
    assert "--json-report" in audit and "UAM_AUDIT_RETENTION_DATABASE_URL" not in audit
    assert "--database-url" not in maintenance
    assert "check_runtime_dependencies.py" in runtime
    assert "--api-key" not in runtime
    conversation = (
        launch_agents / "obelisk-memory/conversation-retention.zsh"
    ).read_text(encoding="utf-8")
    assert "--api-key" not in conversation
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in generated)

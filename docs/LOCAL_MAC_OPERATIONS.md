# Локальные операции на macOS

Для self-hosted Obelisk на Mac используйте user-level `launchd`: он не требует
домена, reverse proxy, VPN или внешнего webhook.

Создайте mode-`0600` файл `~/.config/obelisk-memory/ops.env`. Он должен
содержать пути, а не значения секретов:

```dotenv
OBELISK_PYTHON=/absolute/path/to/universal-agent-memory/.venv/bin/python
OBELISK_EVIDENCE_DIR=/Users/<user>/.local/share/obelisk-memory/evidence
OBELISK_BACKUP_DIR=/Users/<user>/.local/share/obelisk-memory/backups
OBELISK_AUDIT_DIR=/Users/<user>/.local/share/obelisk-memory/audit
UAM_BACKUP_DATABASE_URL=postgresql://... 
UAM_MAINTENANCE_DATABASE_URL=postgresql://...
UAM_BACKUP_ENCRYPTION_KEY_FILE=/Users/<user>/.config/obelisk-memory/backup_encryption_key
UAM_API_KEY_FILE=/Users/<user>/.config/obelisk-memory/operator_api_key
UAM_METRICS_URL=http://127.0.0.1:6798/metrics
PATH=/opt/homebrew/opt/libpq/bin:/opt/homebrew/bin:/usr/bin:/bin
```

Install the jobs:

```bash
python scripts/install_launchd_ops.py \
  --workspace "$PWD" \
  --env-file ~/.config/obelisk-memory/ops.env

for job in backup maintenance metrics; do
  launchctl bootstrap "gui/$(id -u)" \
    "$HOME/Library/LaunchAgents/com.obelisk-memory.$job.plist"
done
```

Schedules are backup daily at 03:23, retention daily at 03:37, and metrics
daily at 09:17. Generated wrappers and logs live under
`~/Library/LaunchAgents/obelisk-memory/`; reports are under
`OBELISK_EVIDENCE_DIR`. `launchctl print gui/$(id -u)/com.obelisk-memory.metrics`
shows the last exit code. Keep backup artifacts on an encrypted external disk
or another operator-controlled durable local volume.

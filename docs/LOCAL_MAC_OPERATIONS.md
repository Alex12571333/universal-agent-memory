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
UAM_BACKUP_SIGNING_KEY_FILE=/Users/<user>/.config/obelisk-memory/backup_signing_key
UAM_BACKUP_SIGNING_KEY_ID=local-backup-key-2026
UAM_API_KEY_FILE=/Users/<user>/.config/obelisk-memory/operator_api_key
UAM_METRICS_URL=http://127.0.0.1:6798/metrics
UAM_INTERNAL_BASE_URL=http://127.0.0.1:6798
UAM_SERVER_ID=00000000-0000-0000-0000-000000000001
UAM_PROJECT_ID=00000000-0000-0000-0000-000000000002
PATH=/usr/local/bin:/opt/homebrew/opt/libpq/bin:/opt/homebrew/bin:/usr/bin:/bin
```

Install the jobs:

```bash
python scripts/install_launchd_ops.py \
  --workspace "$PWD" \
  --env-file ~/.config/obelisk-memory/ops.env

for job in conversation-retention backup maintenance metrics; do
  launchctl bootstrap "gui/$(id -u)" \
    "$HOME/Library/LaunchAgents/com.obelisk-memory.$job.plist"
done
```

Schedules are conversation staging purge daily at 02:47, backup at 03:23,
operational retention at 03:37, and metrics daily at 09:17. Generated wrappers and logs live under
`~/Library/LaunchAgents/obelisk-memory/`; reports are under
`OBELISK_EVIDENCE_DIR`. `launchctl print gui/$(id -u)/com.obelisk-memory.metrics`
shows the last exit code. Keep backup artifacts on an encrypted external disk
or another operator-controlled durable local volume.

On 2026-07-12 the three jobs were installed and manually smoke-tested on the
reference local appliance: metrics and maintenance exited `0`, and the backup
job completed PostgreSQL dump, AES-256-GCM encryption, isolated restore drill
and audit export with exit `0`. On macOS Docker Desktop commonly installs
`docker` under `/usr/local/bin`, hence that directory is required in the job
`PATH` alongside Homebrew `libpq`.

For a production backup job, create a **separate** random signing key (do not
reuse the encryption key) and set the two `UAM_BACKUP_SIGNING_*` entries above.
The scheduled runner then writes and immediately verifies a signed
`*.bundle.json` manifest containing SHA-256 values for the encrypted dump and
the audit manifest. Production restore commands must pass that manifest with
`--require-bundle-signature`; restoration is rejected if the selected dump,
manifest or signature does not verify.

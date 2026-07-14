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
# Optional: defaults to <workspace>/.env when omitted.
OBELISK_RUNTIME_ENV_FILE=/absolute/path/to/universal-agent-memory/.env
UAM_BACKUP_DATABASE_URL_FILE=/Users/<user>/.config/obelisk-memory/backup_database_url
UAM_MAINTENANCE_DATABASE_URL_FILE=/Users/<user>/.config/obelisk-memory/maintenance_database_url
UAM_AUDIT_RETENTION_DATABASE_URL_FILE=/Users/<user>/.config/obelisk-memory/audit_retention_database_url
UAM_BACKUP_ENCRYPTION_KEY_FILE=/Users/<user>/.config/obelisk-memory/backup_encryption_key
UAM_BACKUP_SIGNING_KEY_FILE=/Users/<user>/.config/obelisk-memory/backup_signing_key
UAM_BACKUP_SIGNING_KEY_ID=local-backup-key-2026
UAM_AUDIT_SIGNING_KEY_FILE=/Users/<user>/.config/obelisk-memory/audit_signing_key
UAM_AUDIT_RETENTION_DAYS=365
UAM_API_KEY_FILE=/Users/<user>/.config/obelisk-memory/operator_api_key
UAM_METRICS_URL=http://127.0.0.1:6798/metrics
UAM_INTERNAL_BASE_URL=http://127.0.0.1:6798
UAM_SYSTEM_STATUS_URL=http://127.0.0.1:6798/v1/system/status
# Local notification route; receives a redacted JSON report on stdin.
UAM_ALERT_COMMAND="/absolute/path/to/universal-agent-memory/.venv/bin/python /absolute/path/to/universal-agent-memory/scripts/macos_alert.py"
UAM_SERVER_ID=00000000-0000-0000-0000-000000000001
UAM_PROJECT_ID=00000000-0000-0000-0000-000000000002
PATH=/usr/local/bin:/opt/homebrew/opt/libpq/bin:/opt/homebrew/bin:/usr/bin:/bin
```

`UAM_ALERT_COMMAND` needs neither a domain nor a webhook. On a failed backup or
metrics gate, `macos_alert.py` renders a native macOS notification. An external
webhook remains optional for operators who deliberately configure one.

Install the jobs:

```bash
python scripts/install_launchd_ops.py \
  --workspace "$PWD" \
  --env-file ~/.config/obelisk-memory/ops.env

for job in conversation-retention backup maintenance audit-retention semantic-recovery metrics runtime-dependencies; do
  launchctl bootstrap "gui/$(id -u)" \
    "$HOME/Library/LaunchAgents/com.obelisk-memory.$job.plist"
done
```

Schedules are conversation staging purge daily at 02:47, signed backup at
03:23, operational retention at 04:07, signed audit export-before-prune at
04:37, semantic recovery every Sunday at 05:13, metrics at 09:17, and the
NATS/embedding-worker dependency gate at 09:27. Generated wrappers and logs live under
`~/Library/LaunchAgents/obelisk-memory/`; reports are under
`OBELISK_EVIDENCE_DIR`. `launchctl print gui/$(id -u)/com.obelisk-memory.metrics`
shows the last exit code. Keep backup artifacts on an encrypted external disk
or another operator-controlled durable local volume.

`OBELISK_RUNTIME_ENV_FILE` is read only by the isolated semantic recovery
probe and defaults to `<workspace>/.env` when omitted. It must contain the
local pgcrypto and embedding configuration that the running appliance uses;
keep this file mode `0600`. The weekly job selects the latest encrypted dump,
restores it into temporary PostgreSQL/Qdrant containers, proves dense recall
from the restored ledger, saves timestamped evidence, then removes the
temporary resources.

On 2026-07-12 the then-installed metrics, maintenance, and backup jobs were
manually smoke-tested on the reference local appliance: metrics and maintenance
exited `0`, and backup completed PostgreSQL dump, AES-256-GCM encryption,
isolated restore drill and audit export with exit `0`. The audit-retention and
runtime-dependency schedules added later require their own successful local
evidence before a release may claim them as proven. On macOS Docker Desktop commonly installs
`docker` under `/usr/local/bin`, hence that directory is required in the job
`PATH` alongside Homebrew `libpq`.

For a production backup job, create a **separate** random signing key (do not
reuse the encryption key) and set the two `UAM_BACKUP_SIGNING_*` entries above.
The scheduled runner then writes and immediately verifies a signed
`*.bundle.json` manifest containing SHA-256 values for the encrypted dump and
the audit manifest. Production restore commands must pass that manifest with
`--require-bundle-signature`; restoration is rejected if the selected dump,
manifest or signature does not verify.

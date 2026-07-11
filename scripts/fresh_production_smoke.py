"""Boot an isolated production-shaped stack with generated local secrets."""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=6797)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    project = f"obelisk-smoke-{secrets.token_hex(4)}"
    api_key = "smoke_" + secrets.token_urlsafe(32)
    report: dict[str, object] = {
        "format": "obelisk-fresh-production-smoke-v1",
        "ok": False,
        "project": project,
    }
    with tempfile.TemporaryDirectory(prefix="obelisk-production-smoke-") as temporary:
        root = Path(temporary)
        secrets_dir = root / "secrets"
        secrets_dir.mkdir(mode=0o700)
        env_file = root / ".env"
        override = root / "compose.override.yml"
        paths = {
            name: _secret(secrets_dir, name)
            for name in (
                "postgres_password",
                "app_db_password",
                "ui_session_signing_key",
                "backup_encryption_key",
                "nats_auth_token",
            )
        }
        env_file.write_text(_env(paths, api_key), encoding="utf-8")
        override.write_text(
            f"services:\n  memory-server:\n    ports: !override ['127.0.0.1:{args.port}:8080']\n",
            encoding="utf-8",
        )
        command = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(ROOT / "docker-compose.prod.yml"),
            "-f",
            str(override),
            "--env-file",
            str(env_file),
        ]
        try:
            _run([*command, "build", "--quiet"])
            _run([*command, "up", "-d"])
            _wait_ready(args.port, args.timeout_seconds)
            _run(
                [
                    *command,
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "memory_admin",
                    "-d",
                    "memory",
                    "-c",
                    "select 1",
                ]
            )
            _retain_and_recall(args.port, api_key)
            report["ok"] = True
            print(json.dumps(report))
            return 0
        except Exception as error:
            report["error"] = type(error).__name__
            raise
        finally:
            if not args.keep:
                _run([*command, "down", "-v"], check=False)
            if args.report:
                report["completed_at"] = datetime.now(UTC).isoformat()
                args.report.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")


def _secret(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _env(paths: dict[str, Path], api_key: str) -> str:
    return "\n".join(
        [
            "POSTGRES_DB=memory",
            "POSTGRES_USER=memory_admin",
            "UAM_APP_DB_USER=memory_app",
            "MINIO_ROOT_USER=smoke-minio",
            "MINIO_ROOT_PASSWORD=smoke_" + secrets.token_urlsafe(32),
            "UAM_SERVER_ID=00000000-0000-0000-0000-000000000001",
            "UAM_PROJECT_ID=00000000-0000-0000-0000-000000000002",
            "UAM_VERSION=smoke",
            "UAM_SOURCE_COMMIT=" + "0" * 40,
            "UAM_IMAGE_DIGEST=sha256:" + "0" * 64,
            "UAM_DEPLOYMENT_ID=fresh-smoke",
            "UAM_BUILD_TIME=2026-07-11T00:00:00Z",
            "UAM_API_KEY=" + api_key,
            "UAM_API_KEYS=openclaw:smoke_openclaw_abcdefghijklmnopqrstuvwxyz:agent,hermes:smoke_hermes_abcdefghijklmnopqrstuvwxyz:agent,operator:smoke_operator_abcdefghijklmnopqrstuvwxyz:operator",
            "UAM_REQUIRE_IDENTITY_BINDINGS=false",
            "UAM_EMBEDDING_PROVIDER=fake",
            "UAM_EMBEDDING_MODEL=fake-embed-v1",
            "UAM_EMBEDDING_DIM=1536",
            "UAM_EMBEDDING_BASE_URL=http://host.docker.internal:1/v1",
            "UAM_MEMORY_LLM_PROVIDER=openai-compatible",
            "UAM_MEMORY_LLM_MODEL=fake-memory",
            "UAM_MEMORY_LLM_BASE_URL=http://host.docker.internal:1/v1",
            "UAM_MODEL_ENDPOINT_ALLOWLIST=http://host.docker.internal:1",
            "UAM_MEMORY_TEXT_ENCRYPTION=pgcrypto",
            "UAM_MEMORY_TEXT_ENCRYPTION_SCOPES=all",
            "UAM_MEMORY_TEXT_ENCRYPTION_KEY=smoke_" + secrets.token_urlsafe(32),
            f"POSTGRES_PASSWORD_FILE={paths['postgres_password']}",
            f"UAM_APP_DB_PASSWORD_FILE={paths['app_db_password']}",
            f"UAM_UI_SESSION_SIGNING_KEY_FILE={paths['ui_session_signing_key']}",
            f"UAM_BACKUP_ENCRYPTION_KEY_FILE={paths['backup_encryption_key']}",
            f"UAM_NATS_AUTH_TOKEN_FILE={paths['nats_auth_token']}",
            "",
        ]
    )


def _wait_ready(port: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/ready", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)
    raise RuntimeError("fresh production smoke did not become ready")


def _retain_and_recall(port: int, api_key: str) -> None:
    """Prove API writes and reads work through the generated application role."""
    base = f"http://127.0.0.1:{port}/v1/memory"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    retain = Request(
        base + "/retain",
        data=json.dumps(
            {
                "layer": "semantic",
                "scope": "workspace",
                "kind": "smoke_fact",
                "text": "fresh production smoke marker",
            }
        ).encode(),
        headers=headers,
        method="POST",
    )
    with urlopen(retain, timeout=10):
        pass
    recall = Request(
        base + "/recall",
        data=json.dumps({"query": "fresh production smoke marker"}).encode(),
        headers=headers,
        method="POST",
    )
    with urlopen(recall, timeout=10) as response:
        payload = json.loads(response.read())
    if not any("fresh production smoke marker" in row["text"] for row in payload["results"]):
        raise RuntimeError("fresh production smoke marker was not recallable")


def _run(command: list[str], *, check: bool = True) -> None:
    subprocess.run(command, check=check)


if __name__ == "__main__":
    raise SystemExit(main())

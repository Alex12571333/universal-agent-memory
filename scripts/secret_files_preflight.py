"""Verify that production secrets are mounted through *_FILE paths."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from validate_production_env import parse_env_file

REPORT_FORMAT = "obelisk-secret-files-preflight-v1"
DEFAULT_REQUIRED_SECRETS = (
    "UAM_API_KEY",
    "UAM_API_KEYS",
    "UAM_UI_SESSION_SIGNING_KEY",
    "POSTGRES_PASSWORD",
    "UAM_APP_DB_PASSWORD",
    "MINIO_ROOT_PASSWORD",
    "UAM_MEMORY_TEXT_ENCRYPTION_KEY",
    "UAM_AUDIT_SIGNING_KEY",
    "UAM_VAULT_SIGNING_KEY",
    "UAM_RELEASE_SIGNING_KEY",
    "UAM_MEMORY_LLM_API_KEY",
    "UAM_EMBEDDING_API_KEY",
)
DEFAULT_ALLOWED_PREFIXES = ("/run/secrets",)


def main() -> int:
    """Check mounted secret-file posture and optionally write JSON evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path, help="Production dotenv file to inspect.")
    parser.add_argument(
        "--required-secret",
        action="append",
        default=[],
        help="Secret env name that must be backed by NAME_FILE. Defaults to production set.",
    )
    parser.add_argument(
        "--allowed-prefix",
        action="append",
        default=[],
        help="Allowed absolute directory prefix for mounted secret files.",
    )
    parser.add_argument("--report", type=Path, help="Write JSON release evidence.")
    args = parser.parse_args()

    report = run_preflight(
        env_file=args.env_file,
        required_secrets=tuple(args.required_secret) or DEFAULT_REQUIRED_SECRETS,
        allowed_prefixes=tuple(args.allowed_prefix) or DEFAULT_ALLOWED_PREFIXES,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


def run_preflight(
    *,
    env_file: Path,
    required_secrets: tuple[str, ...] = DEFAULT_REQUIRED_SECRETS,
    allowed_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_PREFIXES,
) -> dict[str, Any]:
    """Return machine-readable evidence for mounted secret-file usage."""
    values = parse_env_file(env_file)
    allowed = tuple(str(Path(prefix).expanduser()) for prefix in allowed_prefixes)
    checks: list[dict[str, Any]] = []
    for secret_name in required_secrets:
        checks.extend(_secret_checks(values, secret_name, allowed))
    return {
        "format": REPORT_FORMAT,
        "ok": all(check["ok"] for check in checks),
        "checked_at": datetime.now(UTC).isoformat(),
        "env_file": str(env_file),
        "required_secrets": list(required_secrets),
        "allowed_prefixes": list(allowed),
        "checks": checks,
    }


def _secret_checks(
    values: dict[str, str],
    secret_name: str,
    allowed_prefixes: tuple[str, ...],
) -> list[dict[str, Any]]:
    raw_value = values.get(secret_name, "").strip()
    file_key = f"{secret_name}_FILE"
    file_value = values.get(file_key, "").strip()
    checks = [
        {
            "name": f"{secret_name}:raw-empty",
            "ok": not raw_value,
            "detail": "raw secret env is empty" if not raw_value else "raw secret env is set",
        },
        {
            "name": f"{secret_name}:file-configured",
            "ok": bool(file_value),
            "detail": f"{file_key} configured" if file_value else f"{file_key} missing",
        },
    ]
    if not file_value:
        checks.extend(
            [
                {
                    "name": f"{secret_name}:file-readable",
                    "ok": False,
                    "detail": "secret file path missing",
                },
                {
                    "name": f"{secret_name}:file-prefix",
                    "ok": False,
                    "detail": "secret file path missing",
                },
            ]
        )
        return checks

    path = Path(file_value).expanduser()
    checks.append(_file_readable_check(secret_name, path))
    checks.append(_file_prefix_check(secret_name, path, allowed_prefixes))
    return checks


def _file_readable_check(secret_name: str, path: Path) -> dict[str, Any]:
    try:
        data = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - report every file failure.
        return {
            "name": f"{secret_name}:file-readable",
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc}",
        }
    return {
        "name": f"{secret_name}:file-readable",
        "ok": bool(data.strip()),
        "detail": "secret file is non-empty" if data.strip() else "secret file is empty",
    }


def _file_prefix_check(
    secret_name: str,
    path: Path,
    allowed_prefixes: tuple[str, ...],
) -> dict[str, Any]:
    absolute = os.path.abspath(path)
    ok = any(
        absolute == prefix or absolute.startswith(prefix.rstrip("/") + os.sep)
        for prefix in allowed_prefixes
    )
    return {
        "name": f"{secret_name}:file-prefix",
        "ok": ok,
        "detail": (
            "secret file is under an allowed prefix"
            if ok
            else f"{absolute} is outside allowed prefixes"
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())

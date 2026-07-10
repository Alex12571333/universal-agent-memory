"""Validate a real `.env.production` before starting Obelisk Memory."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

PLACEHOLDER_PATTERNS = (
    "replace-",
    "changeme",
    "change-me",
    "example",
    "secret",
    "password",
)
VALID_SCOPES = {"admin", "operator", "agent", "read", "write"}


@dataclass(frozen=True, slots=True)
class EnvCheck:
    """One production-env validation check."""

    name: str
    ok: bool
    detail: str


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE dotenv files without expanding variables."""
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"{path}:{line_number}: invalid env key {key!r}")
        values[key] = _strip_quotes(value.strip())
    return values


def validate_env(
    values: dict[str, str],
    *,
    require_public_tls: bool = False,
    require_signed_artifacts: bool = False,
    require_real_embeddings: bool = False,
) -> list[EnvCheck]:
    """Return validation checks for production deployment config."""
    checks = [
        _check_secret(values, "UAM_API_KEY", min_length=32),
        _check_secret(values, "POSTGRES_PASSWORD", min_length=32),
        _check_secret(values, "UAM_APP_DB_PASSWORD", min_length=32),
        _check_secret(values, "MINIO_ROOT_PASSWORD", min_length=32),
        _check_scoped_api_keys(values.get("UAM_API_KEYS", "")),
        _check_uuid(values, "UAM_SERVER_ID"),
        _check_uuid(values, "UAM_PROJECT_ID"),
        _check_context_budget(values),
        _check_privacy(values),
        _check_embedding_dim(values),
        _check_qdrant_payload_text(values),
    ]
    if require_public_tls:
        checks.append(_check_public_tls(values))
    if require_signed_artifacts:
        checks.extend(
            [
                _check_secret(values, "UAM_AUDIT_SIGNING_KEY", min_length=32),
                _check_secret(values, "UAM_VAULT_SIGNING_KEY", min_length=32),
            ]
        )
    if require_real_embeddings:
        checks.append(_check_real_embeddings(values))
    return checks


def _check_secret(values: dict[str, str], key: str, *, min_length: int) -> EnvCheck:
    value = values.get(key, "")
    if not value:
        return EnvCheck(key, False, "missing or empty")
    if len(value) < min_length:
        return EnvCheck(key, False, f"too short; expected at least {min_length} chars")
    lowered = value.lower()
    if any(pattern in lowered for pattern in PLACEHOLDER_PATTERNS):
        return EnvCheck(key, False, "contains placeholder-looking text")
    return EnvCheck(key, True, "configured")


def _check_scoped_api_keys(raw: str) -> EnvCheck:
    if not raw.strip():
        return EnvCheck("UAM_API_KEYS", False, "missing scoped keys")
    names: set[str] = set()
    for entry in raw.split(","):
        parts = entry.split(":")
        if len(parts) != 3:
            return EnvCheck("UAM_API_KEYS", False, f"invalid entry format: {entry!r}")
        name, secret, scopes_raw = (part.strip() for part in parts)
        if not name or name in names:
            return EnvCheck("UAM_API_KEYS", False, f"duplicate or empty key name: {name!r}")
        names.add(name)
        if len(secret) < 24 or any(pattern in secret.lower() for pattern in PLACEHOLDER_PATTERNS):
            return EnvCheck("UAM_API_KEYS", False, f"weak or placeholder secret for {name}")
        scopes = {scope for scope in re.split(r"[+|]", scopes_raw) if scope}
        if not scopes or not scopes <= VALID_SCOPES:
            return EnvCheck("UAM_API_KEYS", False, f"invalid scopes for {name}: {scopes_raw!r}")
    required_names = {"openclaw", "hermes", "operator"}
    missing = sorted(required_names - names)
    if missing:
        return EnvCheck("UAM_API_KEYS", False, f"missing recommended scoped keys: {missing}")
    return EnvCheck("UAM_API_KEYS", True, "scoped keys configured")


def _check_uuid(values: dict[str, str], key: str) -> EnvCheck:
    value = values.get(key, "")
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value,
    ):
        return EnvCheck(key, True, "valid UUID")
    return EnvCheck(key, False, "must be a UUID")


def _check_context_budget(values: dict[str, str]) -> EnvCheck:
    try:
        budget = int(values.get("UAM_CONTEXT_BUDGET_TOKENS", "0"))
    except ValueError:
        return EnvCheck("UAM_CONTEXT_BUDGET_TOKENS", False, "must be an integer")
    if budget < 8192:
        return EnvCheck("UAM_CONTEXT_BUDGET_TOKENS", False, "too small for production recall")
    return EnvCheck("UAM_CONTEXT_BUDGET_TOKENS", True, str(budget))


def _check_privacy(values: dict[str, str]) -> EnvCheck:
    enabled = values.get("UAM_PRIVACY_ENABLED", "").lower()
    action = values.get("UAM_PRIVACY_ACTION", "").lower()
    if enabled != "true":
        return EnvCheck("privacy", False, "UAM_PRIVACY_ENABLED must be true")
    if action not in {"redact", "reject", "metadata_only"}:
        return EnvCheck("privacy", False, "UAM_PRIVACY_ACTION must be redact/reject/metadata_only")
    return EnvCheck("privacy", True, f"{enabled}/{action}")


def _check_embedding_dim(values: dict[str, str]) -> EnvCheck:
    try:
        dimension = int(values.get("UAM_EMBEDDING_DIM", "0"))
    except ValueError:
        return EnvCheck("UAM_EMBEDDING_DIM", False, "must be an integer")
    if dimension <= 0:
        return EnvCheck("UAM_EMBEDDING_DIM", False, "must be positive")
    return EnvCheck("UAM_EMBEDDING_DIM", True, str(dimension))


def _check_qdrant_payload_text(values: dict[str, str]) -> EnvCheck:
    value = values.get("UAM_QDRANT_PAYLOAD_TEXT", "").strip().lower()
    if value != "false":
        return EnvCheck(
            "UAM_QDRANT_PAYLOAD_TEXT",
            False,
            "must be false so Qdrant stores vectors and filters, not raw memory text",
        )
    return EnvCheck("UAM_QDRANT_PAYLOAD_TEXT", True, "raw text redacted from vector payloads")


def _check_public_tls(values: dict[str, str]) -> EnvCheck:
    host = values.get("UAM_PUBLIC_HOST", "")
    email = values.get("UAM_PUBLIC_EMAIL", "")
    if host in {"", "localhost", "127.0.0.1", "::1"}:
        return EnvCheck("public-tls", False, "UAM_PUBLIC_HOST must be a real hostname")
    if "." not in host:
        return EnvCheck("public-tls", False, "UAM_PUBLIC_HOST should be a DNS hostname")
    if "@" not in email:
        return EnvCheck("public-tls", False, "UAM_PUBLIC_EMAIL must be set for ACME")
    return EnvCheck("public-tls", True, host)


def _check_real_embeddings(values: dict[str, str]) -> EnvCheck:
    provider = values.get("UAM_EMBEDDING_PROVIDER", "").lower()
    base_url = values.get("UAM_EMBEDDING_BASE_URL", "")
    if provider in {"", "fake"}:
        return EnvCheck("real-embeddings", False, "fake embeddings are not allowed")
    if not base_url.startswith(("http://", "https://")):
        return EnvCheck("real-embeddings", False, "embedding base URL must be HTTP(S)")
    return EnvCheck("real-embeddings", True, f"{provider} {base_url}")


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def render_checks(checks: Iterable[EnvCheck]) -> str:
    """Render checks as a compact operator report."""
    rows = list(checks)
    lines = [
        f"production_env_valid={str(all(check.ok for check in rows)).lower()}",
    ]
    for check in rows:
        status = "PASS" if check.ok else "FAIL"
        lines.append(f"{status}\t{check.name}\t{check.detail}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path, help="Path to .env.production")
    parser.add_argument("--require-public-tls", action="store_true")
    parser.add_argument("--require-signed-artifacts", action="store_true")
    parser.add_argument("--require-real-embeddings", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    args = parser.parse_args()

    values = parse_env_file(args.env_file)
    checks = validate_env(
        values,
        require_public_tls=args.require_public_tls,
        require_signed_artifacts=args.require_signed_artifacts,
        require_real_embeddings=args.require_real_embeddings,
    )
    ok = all(check.ok for check in checks)
    if args.json:
        print(json.dumps({"ok": ok, "checks": [asdict(check) for check in checks]}, indent=2))
    else:
        print(render_checks(checks))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

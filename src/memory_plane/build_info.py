"""Runtime build identity exposed by the server and release evidence checks."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime

DEFAULT_VERSION = "0.1.0"
BUILD_IDENTITY_FIELDS = (
    "version",
    "source_commit",
    "image_digest",
    "deployment_id",
    "build_time",
)
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
_IMAGE_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-fA-F]{64}")
_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "none",
        "null",
        "unknown",
        "unset",
        "not-set",
        "replace-me",
    }
)


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Immutable identity of the source, image, and deployment serving a request."""

    version: str
    source_commit: str
    image_digest: str
    deployment_id: str
    build_time: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> BuildInfo:
        """Load build identity from the documented ``UAM_*`` environment contract."""
        source = os.environ if environ is None else environ
        return cls(
            version=_env_value(source, "UAM_VERSION", DEFAULT_VERSION),
            source_commit=_env_value(source, "UAM_SOURCE_COMMIT", "unknown"),
            image_digest=_env_value(source, "UAM_IMAGE_DIGEST", "unknown"),
            deployment_id=_env_value(source, "UAM_DEPLOYMENT_ID", "unknown"),
            build_time=_env_value(source, "UAM_BUILD_TIME", "unknown"),
        )

    def public_dict(self) -> dict[str, str]:
        """Return the stable JSON shape used by status and evidence reports."""
        return asdict(self)


def require_build_identity(payload: object) -> dict[str, str]:
    """Validate a status/report build identity or raise ``ValueError``.

    Release evidence must identify the exact source revision and immutable image
    that was exercised. Local servers may expose ``unknown`` defaults, but those
    values intentionally fail this gate.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("build identity is missing or is not an object")

    identity = {
        field: str(payload.get(field, "")).strip()
        for field in BUILD_IDENTITY_FIELDS
    }
    missing = [
        field
        for field, value in identity.items()
        if value.casefold() in _PLACEHOLDER_VALUES
    ]
    if missing:
        raise ValueError(f"build identity has missing/placeholder fields: {', '.join(missing)}")
    if _SOURCE_COMMIT_PATTERN.fullmatch(identity["source_commit"]) is None:
        raise ValueError("build identity source_commit must be a 40-character git SHA")
    if _IMAGE_DIGEST_PATTERN.fullmatch(identity["image_digest"]) is None:
        raise ValueError("build identity image_digest must be sha256:<64 hex characters>")

    try:
        parsed_build_time = datetime.fromisoformat(
            identity["build_time"].replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("build identity build_time must be an ISO-8601 timestamp") from exc
    if parsed_build_time.tzinfo is None:
        raise ValueError("build identity build_time must include a timezone")
    return identity


def require_status_build_identity(payload: object) -> dict[str, str]:
    """Validate status build identity and its duplication in FastAPI version metadata."""
    if not isinstance(payload, Mapping):
        raise ValueError("system status is missing or is not an object")
    identity = require_build_identity(payload.get("build"))
    status_version = str(payload.get("version", "")).strip()
    if status_version != identity["version"]:
        raise ValueError(
            "system status version does not match build identity "
            f"({status_version!r} != {identity['version']!r})"
        )
    return identity


def _env_value(source: Mapping[str, str], name: str, default: str) -> str:
    value = source.get(name, "").strip()
    return value or default

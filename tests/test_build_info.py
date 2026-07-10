from __future__ import annotations

import pytest

from memory_plane.build_info import (
    BuildInfo,
    require_build_identity,
    require_status_build_identity,
)


def _valid_identity() -> dict[str, str]:
    return {
        "version": "1.2.3",
        "source_commit": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "deployment_id": "production-seoul-1",
        "build_time": "2026-07-10T00:00:00Z",
    }


def test_build_info_reads_uam_environment_contract() -> None:
    identity = _valid_identity()
    build = BuildInfo.from_env(
        {
            "UAM_VERSION": identity["version"],
            "UAM_SOURCE_COMMIT": identity["source_commit"],
            "UAM_IMAGE_DIGEST": identity["image_digest"],
            "UAM_DEPLOYMENT_ID": identity["deployment_id"],
            "UAM_BUILD_TIME": identity["build_time"],
        }
    )

    assert build.public_dict() == identity


def test_require_build_identity_accepts_verified_identity() -> None:
    identity = _valid_identity()

    assert require_build_identity(identity) == identity


def test_require_status_build_identity_rejects_version_mismatch() -> None:
    identity = _valid_identity()

    with pytest.raises(ValueError, match="does not match"):
        require_status_build_identity({"version": "9.9.9", "build": identity})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_commit", "unknown", "missing/placeholder"),
        ("source_commit", "main", "git SHA"),
        ("image_digest", "latest", "sha256"),
        ("build_time", "2026-07-10T00:00:00", "timezone"),
    ],
)
def test_require_build_identity_rejects_unverifiable_values(
    field: str,
    value: str,
    message: str,
) -> None:
    identity = _valid_identity()
    identity[field] = value

    with pytest.raises(ValueError, match=message):
        require_build_identity(identity)

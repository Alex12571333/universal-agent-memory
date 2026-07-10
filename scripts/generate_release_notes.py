"""Generate release notes and rollback evidence for production releases."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_FORMAT = "obelisk-release-notes-v1"


def main() -> int:
    """Write release notes JSON with rollback instructions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--previous-ref", required=True)
    parser.add_argument("--current-ref", default="HEAD")
    parser.add_argument("--evidence-manifest", default="release-evidence.json")
    parser.add_argument("--output", type=Path, default=Path("ops/release-notes.json"))
    args = parser.parse_args()

    report = build_release_notes(
        release=args.release,
        previous_ref=args.previous_ref,
        current_ref=args.current_ref,
        evidence_manifest=args.evidence_manifest,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"release_notes={args.output}")
    return 0


def build_release_notes(
    *,
    release: str,
    previous_ref: str,
    current_ref: str,
    evidence_manifest: str,
) -> dict[str, Any]:
    """Return release notes and rollback evidence."""
    previous_commit = _git("rev-parse", previous_ref)
    current_commit = _git("rev-parse", current_ref)
    changelog = _git("log", "--oneline", f"{previous_commit}..{current_commit}")
    commits = [line for line in changelog.splitlines() if line.strip()]
    return {
        "format": REPORT_FORMAT,
        "ok": bool(release and previous_commit and current_commit and commits),
        "generated_at": datetime.now(UTC).isoformat(),
        "release": release,
        "previous_ref": previous_ref,
        "previous_commit": previous_commit,
        "current_ref": current_ref,
        "current_commit": current_commit,
        "evidence_manifest": evidence_manifest,
        "changelog": commits,
        "rollback": [
            "Stop memory-server, outbox-relay and embedding-worker.",
            "Run restore drill against the release backup before touching production data.",
            f"Redeploy the previous image or git ref {previous_commit}.",
            "Restore PostgreSQL from the verified backup only if schema/data rollback is required.",
            "Run migrations, health, metrics, recall smoke and release evidence verification.",
            "Record the rollback in audit/incident notes and rotate any exposed credentials.",
        ],
    }


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())

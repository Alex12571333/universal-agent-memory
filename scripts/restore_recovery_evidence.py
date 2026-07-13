"""Bind Postgres restore, vector reindex and semantic recall into one evidence report."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

RECOVERY_PROBE_FORMAT = "obelisk-restored-reindex-probe-v1"
RESTORE_DRILL_FORMAT = "obelisk-restore-drill-v1"

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-report", type=Path, required=True)
    parser.add_argument("--reindex-report", type=Path, required=True)
    parser.add_argument("--semantic-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    restore = _read(args.restore_report)
    reindex = _read(args.reindex_report)
    semantic = _read(args.semantic_report)
    probe_dimension = reindex.get("embedding_dimension")
    probe_valid = (
        reindex.get("format") == RECOVERY_PROBE_FORMAT
        and semantic.get("format") == RECOVERY_PROBE_FORMAT
        and bool(reindex.get("embedding_model"))
        and isinstance(probe_dimension, int)
        and not isinstance(probe_dimension, bool)
        and probe_dimension > 0
    )
    checks = {
        "restore_drill": _restore_drill_ok(restore),
        "reindex": bool(reindex.get("ok"))
        and int(reindex.get("indexed_points", -1)) == int(reindex.get("verified_points", -2)),
        "semantic_recall": bool(semantic.get("ok"))
        and any(
            item.get("ok") and "semantic" in str(item.get("name", ""))
            for item in semantic.get("checks", [])
        ),
        "canonical_vault_health": bool(reindex.get("ok"))
        and any(
            item.get("name") == "canonical-vault-health" and item.get("ok") is True
            for item in reindex.get("checks", [])
            if isinstance(item, dict)
        ),
        "recovery_probe": probe_valid,
    }
    report = {
        "format": "obelisk-restore-recovery-evidence-v1",
        "ok": all(checks.values()),
        "generated_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "inputs": {
            "restore": str(args.restore_report),
            "reindex": str(args.reindex_report),
            "semantic": str(args.semantic_report),
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _restore_drill_ok(payload: dict) -> bool:
    """Accept legacy scheduled reports and the bound restore-drill proof.

    The new standalone format is stricter: it must attest to schema, forced
    tenant RLS and parity against the source ledger.  Legacy scheduled reports
    remain accepted so pre-existing release evidence can still be verified.
    """
    if payload.get("format") == RESTORE_DRILL_FORMAT:
        checks = payload.get("checks")
        if not isinstance(checks, list) or not payload.get("ok"):
            return False
        successful = {
            item.get("name")
            for item in checks
            if isinstance(item, dict) and item.get("ok") is True
        }
        return {
            "required-schema",
            "forced-tenant-rls",
            "source-row-parity",
        }.issubset(successful)
    return bool(payload.get("ok")) and any(
        isinstance(step, dict)
        and step.get("name") == "restore_drill"
        and step.get("ok") is True
        and step.get("skipped") is not True
        for step in payload.get("steps", [])
    )


if __name__ == "__main__":
    raise SystemExit(main())

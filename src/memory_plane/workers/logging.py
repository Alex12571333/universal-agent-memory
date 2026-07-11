"""Structured, secret-safe worker log events for local Docker operations."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any


def log_event(event: str, **fields: Any) -> None:
    """Write one machine-readable operational event without exception payloads."""
    print(
        json.dumps(
            {"timestamp": datetime.now(UTC).isoformat(), "event": event, **fields},
            sort_keys=True,
            default=str,
        ),
        file=sys.stdout,
        flush=True,
    )

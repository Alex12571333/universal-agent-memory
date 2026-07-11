from __future__ import annotations

import importlib.util
from base64 import b64encode
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "replay_nats_dlq", ROOT / "scripts/replay_nats_dlq.py"
)
assert SPEC and SPEC.loader
replay_nats_dlq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay_nats_dlq)


def test_decode_dlq_record_restores_original_event_bytes() -> None:
    event = b'{"id":"event-1"}'
    raw = (
        '{"source_stream":"MEMORY_EVENTS","source_subject":"memory.events.memory.retained.v1",'
        f'"event_base64":"{b64encode(event).decode()}"}}'
    ).encode()

    record = replay_nats_dlq._decode_record(raw)

    assert record["event"] == event
    assert record["source_stream"] == "MEMORY_EVENTS"
    assert replay_nats_dlq._replay_message_id(event, 4).startswith("replay:4:")

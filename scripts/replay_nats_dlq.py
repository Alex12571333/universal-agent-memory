#!/usr/bin/env python3
"""Replay one operator-selected JetStream DLQ record after fixing its cause."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from base64 import b64decode
from hashlib import sha256
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sequence", type=int, required=True, help="DLQ stream sequence to replay")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate without publishing/deleting"
    )
    parser.add_argument("--nats-url", default=os.getenv("UAM_NATS_URL", "nats://127.0.0.1:6422"))
    parser.add_argument("--dlq-stream", default=os.getenv("UAM_NATS_DLQ_STREAM", "MEMORY_DLQ"))
    return parser.parse_args()


async def replay(
    *, nats_url: str, dlq_stream: str, sequence: int, dry_run: bool
) -> dict[str, object]:
    try:
        import nats
    except ImportError as error:
        raise RuntimeError('NATS support is not installed; use ".[nats]"') from error
    client = await nats.connect(nats_url)
    try:
        jetstream = client.jetstream()
        message = await jetstream.get_msg(dlq_stream, seq=sequence)
        record = _decode_record(message.data)
        result = {"sequence": sequence, "source_stream": record["source_stream"], "replayed": False}
        if dry_run:
            return result
        await jetstream.publish(
            record["source_subject"],
            record["event"],
            stream=record["source_stream"],
            headers={"Nats-Msg-Id": _replay_message_id(record["event"], sequence)},
        )
        await jetstream.delete_msg(dlq_stream, sequence)
        return {**result, "replayed": True}
    finally:
        await client.drain()


def _decode_record(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw)
    required = ("source_stream", "source_subject", "event_base64")
    if not all(isinstance(value.get(key), str) and value[key] for key in required):
        raise ValueError("DLQ record is missing source stream, subject, or event payload")
    return {
        "source_stream": value["source_stream"],
        "source_subject": value["source_subject"],
        "event": b64decode(value["event_base64"], validate=True),
    }


def _replay_message_id(event: bytes, sequence: int) -> str:
    return f"replay:{sequence}:{sha256(event).hexdigest()[:24]}"


def main() -> None:
    args = parse_args()
    result = asyncio.run(
        replay(
            nats_url=args.nats_url,
            dlq_stream=args.dlq_stream,
            sequence=args.sequence,
            dry_run=args.dry_run,
        )
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

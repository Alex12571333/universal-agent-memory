#!/usr/bin/env python3
"""Run an isolated adaptive-recall probe inside the real Hermes runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any


class _RecallStub(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract.
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        self.requests.append({"path": self.path, "payload": payload})
        body = json.dumps(
            {
                "results": [
                    {
                        "layer": "semantic",
                        "text": "TARGET-ADAPTIVE-RECALL-SENTINEL",
                        "score": 0.91,
                        "source": "target-stub",
                    }
                ],
                "context": {
                    "markdown": "- TARGET-ADAPTIVE-RECALL-SENTINEL",
                    "used_tokens": 37,
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    integrations = args.repo.resolve() / "agent-integrations"
    sys.path.insert(0, str(integrations))

    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecallStub)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    os.environ.update(
        {
            "UAM_URL": f"http://127.0.0.1:{server.server_port}",
            "UAM_MEMORY_ENABLED": "true",
            "UAM_RECALL_MODE": "adaptive",
            "UAM_TENANT_ID": "00000000-0000-0000-0000-000000000101",
            "UAM_WORKSPACE_ID": "00000000-0000-0000-0000-000000000102",
            "UAM_AGENT_ID": "00000000-0000-0000-0000-000000000103",
            "UAM_THREAD_ID": "00000000-0000-0000-0000-000000000104",
        }
    )

    from hermes.universal_agent_memory import UniversalAgentMemoryProvider

    provider = UniversalAgentMemoryProvider()
    provider.initialize("isolated-adaptive-recall", platform="target-probe")
    checks: dict[str, bool] = {}
    checks["real_hermes_memory_provider_base"] = (
        UniversalAgentMemoryProvider.__mro__[1].__module__ == "agent.memory_provider"
    )
    checks["greeting_skips_http"] = provider.prefetch("Привет!") == "" and not _RecallStub.requests

    compact = provider.prefetch("Что осталось в нашем проекте?")
    compact_payload = _RecallStub.requests[-1]["payload"]
    checks["compact_recall_injected"] = (
        "TARGET-ADAPTIVE-RECALL-SENTINEL" in compact
        and "untrusted reference data" in compact
        and compact_payload["top_k"] == 6
        and compact_payload["context_budget_tokens"] == 1200
        and compact_payload["context_per_layer_limit"] == 3
        and compact_payload["minimum_score"] == 0.45
    )

    provider._recall_mode = "always"
    full = provider.prefetch("Write a self-contained answer.")
    full_payload = _RecallStub.requests[-1]["payload"]
    checks["explicit_full_tier"] = (
        "TARGET-ADAPTIVE-RECALL-SENTINEL" in full
        and full_payload["top_k"] == 10
        and full_payload["context_budget_tokens"] == 2500
        and full_payload["context_per_layer_limit"] == 6
    )

    metrics = provider.recall_gate_metrics()
    checks["text_free_metrics"] = (
        metrics["decisions"].get("skip:greeting:none") == 1
        and metrics["decisions"].get("recall:project_context:compact") == 1
        and metrics["decisions"].get("recall:mode_always:full") == 1
        and metrics["recalls_total"] == 2
        and metrics["injected_tokens_total"] == 74
        and "проект" not in json.dumps(metrics, ensure_ascii=False).casefold()
    )

    server.shutdown()
    server.server_close()
    provider._url = "http://127.0.0.1:9"
    provider._recall_mode = "always"
    checks["recall_failure_is_fail_soft"] = provider.prefetch("Historical context") == ""

    report = {
        "format": "obelisk-adaptive-recall-target-probe-v1",
        "ok": all(checks.values()),
        "checks": checks,
        "request_count": len(_RecallStub.requests),
        "metrics": metrics,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

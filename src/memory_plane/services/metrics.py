"""Small Prometheus-compatible metrics helpers."""

from __future__ import annotations

from collections.abc import Mapping


def render_prometheus(metrics: Mapping[str, float | int]) -> str:
    """Render numeric metrics in Prometheus text exposition format."""
    lines: list[str] = []
    for name in sorted(metrics):
        value = metrics[name]
        metric_type = "counter" if name.endswith(("_total", "_sum")) else "gauge"
        lines.append(f"# TYPE uam_{name} {metric_type}")
        lines.append(f"uam_{name} {value}")
    return "\n".join(lines) + "\n"

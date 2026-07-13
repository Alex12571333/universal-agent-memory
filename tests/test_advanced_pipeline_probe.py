from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "advanced_pipeline_probe", ROOT / "scripts" / "advanced_pipeline_probe.py"
)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_dependency_health_requires_the_named_healthy_state() -> None:
    dependencies = {
        "nats": {"status": "healthy"},
        "embedding_worker": {"status": "unavailable"},
    }

    assert probe._dependency_is_healthy(dependencies, "nats") is True
    assert probe._dependency_is_healthy(dependencies, "embedding_worker") is False
    assert probe._dependency_is_healthy({}, "nats") is False
    assert probe._dependency_is_healthy(None, "nats") is False

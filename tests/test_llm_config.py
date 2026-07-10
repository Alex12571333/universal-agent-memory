from __future__ import annotations

import json

from memory_plane.adapters.llm import MemoryLLMClient, MemoryLLMConfig, MemoryLLMError


def test_memory_llm_defaults_to_openai_compatible_endpoint(monkeypatch) -> None:
    for name in (
        "UAM_MEMORY_LLM_PROVIDER",
        "UAM_MEMORY_LLM_MODEL",
        "UAM_MEMORY_LLM_BASE_URL",
        "UAM_MEMORY_LLM_API_KEY",
        "UAM_MEMORY_LLM_CONTEXT_TOKENS",
        "UAM_MEMORY_LLM_ENABLE_THINKING",
        "OPENAI_API_KEY",
        "SPARK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    config = MemoryLLMConfig.from_env()

    assert config.provider == "openai-compatible"
    assert config.model_name == "gpt-5.6-terra"
    assert config.base_url == "https://api.openai.com/v1"
    assert config.temperature == 0.1
    assert config.context_window_tokens == 131072
    assert config.enable_thinking is False
    assert config.max_tokens == 1600


def test_memory_llm_reads_openai_api_key_fallback(monkeypatch) -> None:
    monkeypatch.delenv("UAM_MEMORY_LLM_API_KEY", raising=False)
    monkeypatch.delenv("SPARK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")

    config = MemoryLLMConfig.from_env()

    assert config.api_key == "openai-secret"
    assert config.public_dict()["api_key_configured"] is True
    assert config.public_dict()["context_window_tokens"] == 131072


def test_memory_llm_reads_api_key_file(monkeypatch, tmp_path) -> None:
    secret_file = tmp_path / "model_gateway_key"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.delenv("UAM_MEMORY_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SPARK_API_KEY", raising=False)
    monkeypatch.setenv("UAM_MEMORY_LLM_API_KEY_FILE", str(secret_file))

    config = MemoryLLMConfig.from_env()

    assert config.api_key == "file-secret"
    assert config.public_dict()["api_key_configured"] is True


def test_memory_llm_chat_posts_openai_compatible_payload(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": "готово"}}]},
            ).encode()

    def fake_urlopen(request, timeout):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("memory_plane.adapters.llm.urlopen", fake_urlopen)
    config = MemoryLLMConfig(
        provider="openai-compatible",
        model_name="provider/test",
        base_url="https://llm-gateway.example/v1",
        api_key="secret",
        timeout_seconds=9,
        temperature=0.2,
        max_tokens=123,
    )

    result = MemoryLLMClient(config).chat(
        [{"role": "user", "content": "собери контекст памяти"}],
    )

    assert result == "готово"
    assert captured["url"] == "https://llm-gateway.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["payload"] == {
        "model": "provider/test",
        "messages": [{"role": "user", "content": "собери контекст памяти"}],
        "temperature": 0.2,
        "max_tokens": 123,
    }
    assert captured["timeout"] == 9


def test_memory_llm_can_send_qwen_thinking_flag_for_spark_gateways(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": "готово"}}]},
            ).encode()

    def fake_urlopen(request, timeout):  # noqa: ANN001
        captured["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr("memory_plane.adapters.llm.urlopen", fake_urlopen)
    config = MemoryLLMConfig(
        provider="spark",
        model_name="qwen/test",
        base_url="http://192.168.0.10:8000/v1",
    )

    MemoryLLMClient(config).chat([{"role": "user", "content": "собери контекст памяти"}])

    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_memory_llm_chat_json_accepts_fenced_json(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": '```json\n{"keep": true, "score": 0.91}\n```',
                            },
                        },
                    ],
                },
            ).encode()

    monkeypatch.setattr(
        "memory_plane.adapters.llm.urlopen",
        lambda _request, timeout: FakeResponse(),
    )

    result = MemoryLLMClient(MemoryLLMConfig()).chat_json(
        [{"role": "user", "content": "верни json"}],
    )

    assert result == {"keep": True, "score": 0.91}


def test_memory_llm_chat_json_rejects_non_object(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": "[1, 2, 3]"}}]},
            ).encode()

    monkeypatch.setattr(
        "memory_plane.adapters.llm.urlopen",
        lambda _request, timeout: FakeResponse(),
    )

    try:
        MemoryLLMClient(MemoryLLMConfig()).chat_json(
            [{"role": "user", "content": "верни json"}],
        )
    except MemoryLLMError as exc:
        assert "not an object" in str(exc)
    else:
        raise AssertionError("Expected MemoryLLMError")


def test_memory_llm_chat_explains_null_content(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "reasoning": "still thinking",
                            },
                        },
                    ],
                },
            ).encode()

    monkeypatch.setattr(
        "memory_plane.adapters.llm.urlopen",
        lambda _request, timeout: FakeResponse(),
    )

    try:
        MemoryLLMClient(MemoryLLMConfig()).chat(
            [{"role": "user", "content": "короткий smoke test"}],
        )
    except MemoryLLMError as exc:
        assert "max_tokens" in str(exc)
    else:
        raise AssertionError("Expected MemoryLLMError")

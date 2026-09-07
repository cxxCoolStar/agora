import httpx

from agora.provider import CodexProvider


def test_responses_payload_and_function_call_parsing(monkeypatch):
    captured = {}

    class FakeResponse:
        is_error = False

        def json(self):
            return {
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "read_file",
                        "arguments": '{"path":"notes.txt"}',
                    }
                ]
            }

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            captured.update({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    provider = CodexProvider({"base_url": "https://api.example.test/v1", "api_key": "secret", "model": "gpt-5.6-terra"}, timeout=17)

    import asyncio

    response = asyncio.run(
        provider.complete_with_tools(
            [{"role": "user", "content": "read notes"}],
            [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}],
        )
    )

    assert captured["url"] == "https://api.example.test/v1/responses"
    assert captured["timeout"] == 17
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"] == {
        "model": "gpt-5.6-terra",
        "input": [{"role": "user", "content": "read notes"}],
        "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}],
        "stream": False,
    }
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == '{"path":"notes.txt"}'


def test_default_timeout_is_bounded(monkeypatch):
    monkeypatch.delenv("AGORA_LLM_TIMEOUT", raising=False)
    provider = CodexProvider({"base_url": "https://api.example.test/v1", "api_key": "secret", "model": "gpt-5.6-luna"})
    assert provider.timeout == 45.0


def test_short_env_names_load_openai_chat_configuration(tmp_path, monkeypatch):
    from agora.config import load_provider_config

    (tmp_path / ".env").write_text("LLM_APIKEY=secret\nBASE_URL=https://llm.example/v1\nMODEL=my-model\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in ("AGORA_API_KEY", "AGORA_BASE_URL", "AGORA_MODEL", "AGORA_API_TYPE", "LLM_APIKEY", "BASE_URL", "MODEL"):
        monkeypatch.delenv(name, raising=False)
    settings = load_provider_config()
    assert settings["api_key"] == "secret"
    assert settings["base_url"] == "https://llm.example/v1"
    assert settings["model"] == "my-model"
    assert settings["api_type"] == "openai-chat"


def test_openai_chat_completions_payload_and_tool_parsing(monkeypatch):
    captured = {}

    class FakeResponse:
        is_error = False
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"notes.txt"}'}}]}}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, url, *, headers, json):
            captured.update({"url": url, "json": json})
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    provider = CodexProvider({"base_url": "https://api.example/v1", "api_key": "secret", "model": "m", "api_type": "openai-chat"})
    import asyncio
    response = asyncio.run(provider.complete_with_tools([{"role": "user", "content": "read"}], [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]))
    assert captured["url"] == "https://api.example/v1/chat/completions"
    assert captured["json"]["messages"][0]["role"] == "user"
    assert captured["json"]["tools"][0]["function"]["name"] == "read_file"
    assert response.tool_calls[0].name == "read_file"

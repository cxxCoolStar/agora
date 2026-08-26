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

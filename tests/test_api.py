from pathlib import Path

from fastapi.testclient import TestClient

from agora.main import create_app
from agora.provider import ModelResponse, ToolCall
from agora.web_search import WebSearchService


class FakeProvider:
    model = "gpt-5.6-luna"
    configured = True

    async def complete(self, messages):
        return "real provider response"


def test_chat_persists_final_message(tmp_path: Path):
    with TestClient(create_app(tmp_path / "data", tmp_path, provider=FakeProvider())) as client:
        response = client.post("/api/chat/stream", json={"agentId": "default", "sessionId": "s1", "message": "hello"})
        assert response.status_code == 200
        assert '"type": "done"' in response.text
        history = client.get("/api/chat/history", params={"agentId": "default", "sessionId": "s1"}).json()
        assert [item["role"] for item in history["history"]] == ["user", "assistant"]
        assert history["history"][-1]["content"] == "real provider response"


def test_frontend_root_is_served_without_shadowing_api(tmp_path: Path):
    with TestClient(create_app(tmp_path / "data", tmp_path, provider=FakeProvider())) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "<html" in page.text.lower()
        wildcard_route = client.get("/**")
        assert wildcard_route.status_code == 200
        assert "<html" in wildcard_route.text.lower()
        assert client.get("/api/status").json()["running"] is True


def test_default_model_is_codex_gpt_5_6_luna(tmp_path: Path):
    with TestClient(create_app(tmp_path / "data", tmp_path, provider=FakeProvider())) as client:
        assert client.get("/api/status").json()["agents"][0]["model"] == "gpt-5.6-luna"
        assert client.get("/api/agents").json()["agents"][0]["model"] == "gpt-5.6-luna"


def test_unconfigured_provider_returns_failed_terminal_event(tmp_path: Path):
    class UnconfiguredProvider:
        model = "gpt-5.6-luna"
        configured = False

        async def complete(self, messages):
            raise RuntimeError("provider unavailable")

    with TestClient(create_app(tmp_path / "data", tmp_path, provider=UnconfiguredProvider())) as client:
        response = client.post("/api/chat/stream", json={"agentId": "default", "sessionId": "s1", "message": "hello"})
        assert '"type": "error"' in response.text
        assert '"state": "failed"' in response.text


def test_responses_tool_call_executes_and_returns_final_answer(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("Agora has tool calls.", encoding="utf8")

    class ToolProvider:
        model = "gpt-5.6-luna"
        configured = True

        def __init__(self):
            self.requests = []

        async def complete(self, messages):
            raise AssertionError("tool-capable provider should use complete_with_tools")

        async def complete_with_tools(self, messages, tools):
            self.requests.append((messages, tools))
            if len(self.requests) == 1:
                assert {tool["name"] for tool in tools} == {"list_dir", "read_file", "write_file", "patch", "search_files"}
                return ModelResponse(
                    text="",
                    tool_calls=(ToolCall("call-1", "read_file", '{"path":"notes.txt"}'),),
                    output_items=({"type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "read_file", "arguments": '{"path":"notes.txt"}'},),
                )
            output = messages[-1]
            assert output["type"] == "function_call_output"
            assert "Agora has tool calls" in output["output"]
            return ModelResponse(text="The file confirms that Agora has tool calls.")

    provider = ToolProvider()
    with TestClient(create_app(tmp_path / "data", tmp_path, provider=provider, web_search=WebSearchService([]))) as client:
        response = client.post("/api/chat/stream", json={"agentId": "default", "sessionId": "s1", "message": "read the note"})
        assert '"type": "tool_call"' in response.text
        assert '"type": "tool_result"' in response.text
        assert "The file confirms that Agora has tool calls." in response.text
        executions = client.app.state.store.connection.execute("SELECT name,state FROM tool_executions").fetchall()
        assert [(row["name"], row["state"]) for row in executions] == [("read_file", "completed")]
        history = client.get("/api/chat/history", params={"agentId": "default", "sessionId": "s1"}).json()
        assert history["history"][-1]["content"] == "The file confirms that Agora has tool calls."


def test_tool_execution_exception_is_persisted_as_failed(tmp_path: Path):
    class BrokenToolsProvider:
        model = "gpt-5.6-luna"
        configured = True

        async def complete(self, messages):
            return "unused"

        async def complete_with_tools(self, messages, tools):
            if not any(item.get("type") == "function_call_output" for item in messages if isinstance(item, dict)):
                return ModelResponse(
                    text="",
                    tool_calls=(ToolCall("call-1", "read_file", '{"path":"notes.txt"}'),),
                    output_items=({"type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "read_file", "arguments": '{"path":"notes.txt"}'},),
                )
            return ModelResponse(text="The tool failed and I reported it honestly.")

    (tmp_path / "notes.txt").write_bytes(b"\xff")
    with TestClient(create_app(tmp_path / "data", tmp_path, provider=BrokenToolsProvider())) as client:
        response = client.post("/api/chat/stream", json={"agentId": "default", "sessionId": "s1", "message": "read the note"})
        assert response.status_code == 200
        executions = client.app.state.store.connection.execute("SELECT state,result FROM tool_executions").fetchall()
        assert executions[0]["state"] == "failed"
        assert "UTF-8" in executions[0]["result"]


def test_web_search_is_available_to_the_agent_only_when_configured(tmp_path: Path):
    class StubSearch:
        available = True

        async def search(self, query, count=5):
            assert query == "Agora agent"
            assert count == 2
            return {"query": query, "provider": "test", "results": [{"title": "Agora", "url": "https://example.test/agora", "snippet": "A test result."}]}

    class SearchProvider:
        model = "gpt-5.6-luna"
        configured = True

        async def complete(self, messages):
            raise AssertionError("tool-capable provider should use complete_with_tools")

        async def complete_with_tools(self, messages, tools):
            if not any(item.get("type") == "function_call_output" for item in messages if isinstance(item, dict)):
                assert "web_search" in {tool["name"] for tool in tools}
                return ModelResponse(
                    text="",
                    tool_calls=(ToolCall("call-web", "web_search", '{"query":"Agora agent","count":2}'),),
                    output_items=({"type": "function_call", "id": "fc-web", "call_id": "call-web", "name": "web_search", "arguments": '{"query":"Agora agent","count":2}'},),
                )
            assert "https://example.test/agora" in messages[-1]["output"]
            return ModelResponse(text="I found Agora in the configured web search provider.")

    with TestClient(create_app(tmp_path / "data", tmp_path, provider=SearchProvider(), web_search=StubSearch())) as client:
        response = client.post("/api/chat/stream", json={"agentId": "default", "sessionId": "s1", "message": "search the web"})
        assert response.status_code == 200
        assert '"name": "web_search"' in response.text
        assert "I found Agora" in response.text

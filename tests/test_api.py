from pathlib import Path

from fastapi.testclient import TestClient

from agora.main import create_app


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

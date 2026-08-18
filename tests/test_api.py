from pathlib import Path

from fastapi.testclient import TestClient

from agora.main import create_app


def test_chat_persists_final_message(tmp_path: Path):
    with TestClient(create_app(tmp_path / "data", tmp_path)) as client:
        response = client.post("/api/chat/stream", json={"agentId": "default", "sessionId": "s1", "message": "hello"})
        assert response.status_code == 200
        assert '"type": "done"' in response.text
        history = client.get("/api/chat/history", params={"agentId": "default", "sessionId": "s1"}).json()
        assert [item["role"] for item in history["history"]] == ["user", "assistant"]


def test_frontend_root_is_served_without_shadowing_api(tmp_path: Path):
    with TestClient(create_app(tmp_path / "data", tmp_path)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "<html" in page.text.lower()
        wildcard_route = client.get("/**")
        assert wildcard_route.status_code == 200
        assert "<html" in wildcard_route.text.lower()
        assert client.get("/api/status").json()["running"] is True


def test_default_model_is_codex_gpt_5_4_mini(tmp_path: Path):
    with TestClient(create_app(tmp_path / "data", tmp_path)) as client:
        assert client.get("/api/status").json()["agents"][0]["model"] == "gpt-5.4-mini"
        assert client.get("/api/agents").json()["agents"][0]["model"] == "gpt-5.4-mini"

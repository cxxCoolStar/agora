import asyncio

import pytest

from agora.tool_registry import ToolRegistry
from agora.tools import ToolError, WorkspaceTools
from agora.web_search import TavilySearchProvider, WebSearchError, WebSearchResult, WebSearchService, load_web_search_service


class FakeSearchProvider:
    def __init__(self, name: str, *, configured: bool = True, failure: str | None = None) -> None:
        self.name = name
        self.configured = configured
        self.failure = failure
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, count: int) -> list[WebSearchResult]:
        self.calls.append((query, count))
        if self.failure:
            raise WebSearchError(self.failure)
        return [WebSearchResult("Agora", "https://example.test/agora", "A test result")]


def test_web_search_falls_back_to_next_configured_provider():
    first = FakeSearchProvider("first", failure="temporary outage")
    second = FakeSearchProvider("second")
    service = WebSearchService([first, second])

    result = asyncio.run(service.search("Agora", count=30))

    assert result["provider"] == "second"
    assert result["results"][0]["url"] == "https://example.test/agora"
    assert first.calls == [("Agora", 20)]
    assert second.calls == [("Agora", 20)]


def test_unconfigured_web_search_is_not_registered_or_executable(tmp_path):
    registry = ToolRegistry(WorkspaceTools(tmp_path), WebSearchService([]))

    assert "web_search" not in {definition["name"] for definition in registry.definitions()}
    with pytest.raises(ToolError, match="no web search provider"):
        asyncio.run(registry.execute("web_search", {"query": "Agora"}))


def test_tavily_provider_posts_search_request(monkeypatch):
    captured = {}

    class FakeResponse:
        is_error = False
        text = ""

        def json(self):
            return {"results": [{"title": "Agora", "url": "https://example.test/agora", "content": "A result"}]}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json, headers):
            captured.update({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr("agora.web_search.httpx.AsyncClient", FakeClient)
    provider = TavilySearchProvider("secret", timeout=9)
    result = asyncio.run(provider.search("Agora", 3))

    assert result[0].url == "https://example.test/agora"
    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["timeout"] == 9
    assert captured["json"]["api_key"] == "secret"
    assert captured["json"]["max_results"] == 3


def test_tavily_key_alias_is_loaded_from_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TAVILY_KEY=secret\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_KEY", raising=False)
    monkeypatch.delenv("AGORA_WEB_SEARCH_PROVIDERS", raising=False)

    service = load_web_search_service()
    assert service.available is True
    assert service.providers[0].name == "tavily"
    assert service.providers[0].api_key == "secret"

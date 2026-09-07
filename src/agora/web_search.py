from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx


class WebSearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class WebSearchProvider(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    async def search(self, query: str, count: int) -> list[WebSearchResult]: ...


def _clean_text(value: object, *, limit: int) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


class BraveSearchProvider:
    name = "brave"

    def __init__(self, api_key: str | None, endpoint: str | None = None, timeout: float = 15) -> None:
        self.api_key = (api_key or "").strip()
        self.endpoint = (endpoint or "https://api.search.brave.com/res/v1/web/search").rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, count: int) -> list[WebSearchResult]:
        headers = {"Accept": "application/json", "X-Subscription-Token": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.endpoint, params={"q": query, "count": count}, headers=headers)
        except httpx.HTTPError as exc:
            raise WebSearchError(f"brave request failed: {exc}") from exc
        if response.is_error:
            raise WebSearchError(f"brave returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            rows = response.json().get("web", {}).get("results", [])
        except (ValueError, AttributeError) as exc:
            raise WebSearchError("brave returned invalid JSON") from exc
        return _normalize_results(rows, title="title", url="url", snippet="description", count=count)


class TavilySearchProvider:
    """Tavily web search provider using Tavily's native search endpoint."""

    name = "tavily"

    def __init__(self, api_key: str | None, endpoint: str | None = None, timeout: float = 15) -> None:
        self.api_key = (api_key or "").strip()
        self.endpoint = (endpoint or "https://api.tavily.com/search").rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, count: int) -> list[WebSearchResult]:
        if not self.configured:
            raise WebSearchError("tavily is not configured")
        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": count,
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.endpoint,
                    json=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise WebSearchError(f"tavily request failed: {exc}") from exc
        if response.is_error:
            raise WebSearchError(f"tavily returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            rows = response.json().get("results", [])
        except (ValueError, AttributeError) as exc:
            raise WebSearchError("tavily returned invalid JSON") from exc
        return _normalize_results(rows, title="title", url="url", snippet="content", count=count)


class SearxNGSearchProvider:
    name = "searxng"

    def __init__(self, endpoint: str | None, timeout: float = 15) -> None:
        self.endpoint = (endpoint or "").rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    async def search(self, query: str, count: int) -> list[WebSearchResult]:
        if not self.configured:
            raise WebSearchError("searxng is not configured")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.endpoint}/search",
                    params={"q": query, "format": "json"},
                    headers={"Accept": "application/json", "User-Agent": "Agora/0.1"},
                )
        except httpx.HTTPError as exc:
            raise WebSearchError(f"searxng request failed: {exc}") from exc
        if response.is_error:
            raise WebSearchError(f"searxng returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            rows = response.json().get("results", [])
        except (ValueError, AttributeError) as exc:
            raise WebSearchError("searxng returned invalid JSON") from exc
        return _normalize_results(rows, title="title", url="url", snippet="content", count=count)


def _normalize_results(rows: object, *, title: str, url: str, snippet: str, count: int) -> list[WebSearchResult]:
    if not isinstance(rows, list):
        raise WebSearchError("search provider returned an invalid result list")
    results: list[WebSearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        link = _clean_text(row.get(url), limit=2_048)
        if not link.startswith(("https://", "http://")):
            continue
        results.append(WebSearchResult(_clean_text(row.get(title), limit=300), link, _clean_text(row.get(snippet), limit=1_000)))
        if len(results) == count:
            break
    return results


class WebSearchService:
    """Routes web_search through configured providers, with bounded fallback."""

    def __init__(self, providers: list[WebSearchProvider]) -> None:
        self.providers = providers

    @property
    def available(self) -> bool:
        return any(provider.configured for provider in self.providers)

    async def search(self, query: str, count: int = 5) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise WebSearchError("query must be a non-empty string")
        count = max(1, min(int(count), 20))
        failures: list[str] = []
        for provider in self.providers:
            if not provider.configured:
                continue
            try:
                results = await provider.search(query, count)
            except WebSearchError as exc:
                failures.append(str(exc))
                continue
            return {"query": query, "provider": provider.name, "results": [result.to_dict() for result in results]}
        if failures:
            raise WebSearchError("web search failed: " + "; ".join(failures))
        raise WebSearchError("web search is not configured; set AGORA_WEB_SEARCH_PROVIDERS and provider credentials")


def load_web_search_service() -> WebSearchService:
    # Keep .env convenient for local development without requiring an extra
    # dependency. Process environment variables always take precedence.
    dotenv = _load_dotenv(Path.cwd() / ".env")

    def env(name: str, *aliases: str) -> str | None:
        for key in (name, *aliases):
            value = os.getenv(key) or dotenv.get(key)
            if value and value.strip():
                return value.strip()
        return None

    configured_order = (env("AGORA_WEB_SEARCH_PROVIDERS") or "").strip()
    names = [name.strip().lower() for name in configured_order.split(",") if name.strip()]
    providers: dict[str, WebSearchProvider] = {
        "tavily": TavilySearchProvider(env("TAVILY_API_KEY", "TAVILY_KEY"), env("TAVILY_API_URL")),
        "brave": BraveSearchProvider(env("AGORA_BRAVE_SEARCH_API_KEY"), env("AGORA_BRAVE_SEARCH_URL")),
        "searxng": SearxNGSearchProvider(env("AGORA_SEARXNG_URL")),
    }
    if names:
        unknown = [name for name in names if name not in providers]
        if unknown:
            raise WebSearchError(f"unsupported AGORA_WEB_SEARCH_PROVIDERS value: {', '.join(unknown)}")
        return WebSearchService([providers[name] for name in names])
    return WebSearchService([providers["tavily"], providers["brave"], providers["searxng"]])


def _load_dotenv(path: Path) -> dict[str, str]:
    """Read simple KEY=value entries from a local .env file.

    This intentionally supports only the common dotenv form and never logs
    values. It is a fallback for local startup; real environment variables
    remain authoritative.
    """
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values

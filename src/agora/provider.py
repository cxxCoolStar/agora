from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from .config import load_provider_config


class ProviderError(RuntimeError):
    pass


class LLMProvider(Protocol):
    model: str

    async def complete(self, messages: Sequence[dict[str, str]]) -> str: ...


class CodexProvider:
    """OpenAI Responses-wire provider backed by the local .codex configuration."""

    def __init__(self, config: dict[str, str | None] | None = None, timeout: float = 120.0) -> None:
        settings = config or load_provider_config()
        self.base_url = (settings.get("base_url") or "").rstrip("/")
        self.api_key = settings.get("api_key") or ""
        self.model = settings.get("model") or "gpt-5.6-luna"
        self.provider = settings.get("provider")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    async def complete(self, messages: Sequence[dict[str, str]]) -> str:
        if not self.configured:
            raise ProviderError("LLM provider is not configured; set AGORA_API_KEY and AGORA_BASE_URL or configure ~/.codex/config.toml")
        payload = {
            "model": self.model,
            "input": [{"role": item["role"], "content": item["content"]} for item in messages],
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/responses", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"LLM request failed: {exc}") from exc
        if response.is_error:
            detail = response.text[:500]
            raise ProviderError(f"LLM provider returned HTTP {response.status_code}: {detail}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("LLM provider returned invalid JSON") from exc
        text = self._extract_text(body)
        if not text:
            raise ProviderError("LLM provider returned no output text")
        return text

    @classmethod
    def _extract_text(cls, body: Any) -> str:
        if isinstance(body, dict) and isinstance(body.get("output_text"), str):
            return body["output_text"]
        chunks: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if value.get("type") in {"output_text", "text"} and isinstance(value.get("text"), str):
                    chunks.append(value["text"])
                for key, child in value.items():
                    if key not in {"input", "arguments"}:
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(body.get("output", body) if isinstance(body, dict) else body)
        return "".join(chunks).strip()

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import json

from .config import load_provider_config


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    output_items: tuple[dict[str, Any], ...] = ()


class LLMProvider(Protocol):
    model: str

    async def complete(self, messages: Sequence[dict[str, Any]]) -> str: ...

    async def complete_with_tools(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelResponse: ...


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

    async def complete(self, messages: Sequence[dict[str, Any]]) -> str:
        response = await self.complete_with_tools(messages, ())
        if response.tool_calls:
            raise ProviderError("LLM provider returned tool calls without an available tool executor")
        if not response.text:
            raise ProviderError("LLM provider returned no output text")
        return response.text

    async def complete_with_tools(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelResponse:
        if not self.configured:
            raise ProviderError("LLM provider is not configured; set AGORA_API_KEY and AGORA_BASE_URL or configure ~/.codex/config.toml")
        payload = {
            "model": self.model,
            "input": list(messages),
            "stream": False,
        }
        if tools:
            payload["tools"] = list(tools)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/responses", headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            detail = str(exc) or "request timed out"
            raise ProviderError(f"LLM request timed out at {self.base_url}/responses: {detail}") from exc
        except httpx.ConnectError as exc:
            detail = str(exc) or "connection could not be established"
            raise ProviderError(f"LLM connection failed to {self.base_url}/responses: {detail}") from exc
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise ProviderError(f"LLM request failed at {self.base_url}/responses: {detail}") from exc
        if response.is_error:
            detail = response.text[:500]
            raise ProviderError(f"LLM provider returned HTTP {response.status_code}: {detail}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("LLM provider returned invalid JSON") from exc
        output = body.get("output", []) if isinstance(body, dict) else []
        if not isinstance(output, list):
            output = []
        return ModelResponse(
            text=self._extract_text(body),
            tool_calls=tuple(self._extract_tool_calls(output)),
            output_items=tuple(item for item in output if isinstance(item, dict) and item.get("type") == "function_call"),
        )

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

    @staticmethod
    def _extract_tool_calls(output: Sequence[Any]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            name = item.get("name")
            call_id = item.get("call_id") or item.get("id")
            arguments = item.get("arguments", "{}")
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments, ensure_ascii=False)
            if isinstance(name, str) and isinstance(call_id, str) and isinstance(arguments, str):
                calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
        return calls

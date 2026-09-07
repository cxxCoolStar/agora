from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import json
import os

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

    def __init__(self, config: dict[str, str | None] | None = None, timeout: float | None = None) -> None:
        settings = config or load_provider_config()
        self.base_url = (settings.get("base_url") or "").rstrip("/")
        self.api_key = settings.get("api_key") or ""
        self.model = settings.get("model") or "gpt-5.6-luna"
        self.provider = settings.get("provider")
        self.api_type = settings.get("api_type") or ("responses" if config is not None else "openai-chat")
        configured_timeout = os.getenv("AGORA_LLM_TIMEOUT", "45")
        try:
            default_timeout = max(5.0, float(configured_timeout))
        except ValueError:
            default_timeout = 45.0
        self.timeout = timeout if timeout is not None else default_timeout

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
            raise ProviderError("LLM provider is not configured; set LLM_APIKEY and BASE_URL (or AGORA_API_KEY and AGORA_BASE_URL)")
        if self.api_type in {"openai-chat", "chat", "chat-completions", "chat_completions"}:
            return await self._complete_chat(messages, tools)
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

    async def _complete_chat(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]) -> ModelResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": list(messages), "stream": False}
        if tools:
            payload["tools"] = [self._chat_tool_definition(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        endpoint = f"{self.base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"LLM request timed out at {endpoint}: {exc or 'request timed out'}") from exc
        except httpx.ConnectError as exc:
            raise ProviderError(f"LLM connection failed to {endpoint}: {exc or 'connection could not be established'}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"LLM request failed at {endpoint}: {exc or type(exc).__name__}") from exc
        if response.is_error:
            raise ProviderError(f"LLM provider returned HTTP {response.status_code}: {response.text[:500]}")
        try:
            body = response.json()
            choice = body.get("choices", [])[0]
            message = choice.get("message", {})
        except (ValueError, AttributeError, IndexError, TypeError) as exc:
            raise ProviderError("LLM provider returned invalid Chat Completions JSON") from exc
        text = self._chat_message_text(message.get("content"))
        calls: list[ToolCall] = []
        for item in message.get("tool_calls", []) or []:
            function = item.get("function", {}) if isinstance(item, dict) else {}
            call_id, name, arguments = item.get("id"), function.get("name"), function.get("arguments", "{}")
            if isinstance(call_id, str) and isinstance(name, str):
                if isinstance(arguments, (dict, list)):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                if isinstance(arguments, str):
                    calls.append(ToolCall(call_id, name, arguments))
        output_items: tuple[dict[str, Any], ...] = ()
        if calls:
            output_items = ({"role": "assistant", "content": message.get("content"), "tool_calls": message.get("tool_calls", [])},)
        return ModelResponse(text=text, tool_calls=tuple(calls), output_items=output_items)

    @staticmethod
    def _chat_tool_definition(tool: dict[str, Any]) -> dict[str, Any]:
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            return tool
        return {"type": "function", "function": {key: tool[key] for key in ("name", "description", "parameters") if key in tool}}

    @staticmethod
    def _chat_message_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(item.get("text", "") for item in content if isinstance(item, dict)).strip()
        return ""

    def tool_result_message(self, call: ToolCall, result: str) -> dict[str, Any]:
        if self.api_type in {"openai-chat", "chat", "chat-completions", "chat_completions"}:
            return {"role": "tool", "tool_call_id": call.id, "content": result}
        return {"type": "function_call_output", "call_id": call.id, "output": result}

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

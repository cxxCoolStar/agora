from __future__ import annotations

import asyncio
from typing import Any

from .tools import ToolError, WorkspaceTools
from .web_search import WebSearchError, WebSearchService


class ToolRegistry:
    """The Agent-facing tool catalog; individual tool families stay isolated."""

    def __init__(self, workspace: WorkspaceTools, web_search: WebSearchService | None = None) -> None:
        self.workspace = workspace
        self.web_search = web_search

    def definitions(self) -> list[dict[str, Any]]:
        definitions = self.workspace.definitions()
        if self.web_search and self.web_search.available:
            definitions.append(
                {
                    "type": "function",
                    "name": "web_search",
                    "description": "Search the public web for current information. Use when local workspace files cannot answer. Returns untrusted titles, URLs, and snippets; verify important claims against cited sources.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Specific web search query."},
                            "count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }
            )
        return definitions

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "web_search":
            if not self.web_search or not self.web_search.available:
                raise ToolError("web_search is unavailable because no web search provider is configured")
            try:
                return await self.web_search.search(**arguments)
            except (TypeError, ValueError, WebSearchError) as exc:
                raise ToolError(str(exc)) from exc
        return await asyncio.to_thread(self.workspace.execute, name, arguments)

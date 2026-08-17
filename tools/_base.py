"""Base tool — every source tool subclasses this.

Tools are deterministic Python functions with Pydantic I/O and typed failures.
A tool returns SourceResult on success or ToolFailure on failure — failures are
DATA the orchestrator routes around, never exceptions that escape into the loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from ledger.models import SourceResult, ToolFailure
from tools._transport import HttpRequest, Transport, TransportError


class BaseTool(ABC):
    name: str
    description: str
    args_schema: type[BaseModel]  # Pydantic model -> Anthropic tool input_schema
    # Env vars this tool needs at runtime. Declared per-tool so run() validates the
    # union (mode-aware: live mode requires them, demo/replay mode never sets them).
    required_env: list[str] = []

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    @abstractmethod
    async def run(self, **kwargs: Any) -> SourceResult | ToolFailure:
        """Execute the tool. Never raise — return ToolFailure on error."""
        ...

    def schema(self) -> dict:
        """Anthropic tool_use schema for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.args_schema.model_json_schema(),
        }

    def _failure(self, reason: str, suggestion: str = "", retriable: bool = False) -> ToolFailure:
        return ToolFailure(
            tool=self.name, reason=reason, suggestion=suggestion, retriable=retriable
        )

    async def _request(self, req: HttpRequest) -> str | ToolFailure:
        """Call the transport; return body text or ToolFailure (never raise)."""
        try:
            resp = await self._transport.request(req)
        except TransportError as exc:
            return self._failure(str(exc), suggestion="retry or skip this source", retriable=True)
        if resp.status >= 400:
            return self._failure(
                f"HTTP {resp.status} from {req.url}",
                suggestion="skip this source; note it in the brief",
                retriable=resp.status >= 500,
            )
        return resp.body

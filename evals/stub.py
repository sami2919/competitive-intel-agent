"""Stub Anthropic client + block types for no-key loop/extractor tests.

Mirrors the SDK shape the loop reads (block.type/.text/.name/.input/.id, resp.usage,
resp.stop_reason) so the loop is identical against the real client or the stub.
The stub branches on model: haiku calls return the extraction JSON; sonnet calls
return the next scripted orchestration response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class StubUsage:
    input_tokens: int = 100
    output_tokens: int = 200
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class StubResponse:
    content: list
    usage: StubUsage = field(default_factory=StubUsage)
    stop_reason: str = "end_turn"


class _StubMessages:
    def __init__(self, sonnet_script: list[StubResponse], haiku_text: str) -> None:
        self._sonnet = sonnet_script
        self._i = 0
        self._haiku = StubResponse(content=[TextBlock(text=haiku_text)])
        # Phase 7: record every create() call so behavioral tests can assert which prompt
        # a call used and whether it was prose-only create_text (tools=[], thinking
        # disabled) vs a tool-use create(). Additive — existing tests ignore it.
        self.calls: list[dict] = []

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: Any,
        messages: Any,
        tools: Any = None,
        tool_choice: Any = None,
        thinking: Any = None,
    ) -> StubResponse:
        sys_text = ""
        if isinstance(system, list) and system and isinstance(system[0], dict):
            sys_text = system[0].get("text", "")
        elif isinstance(system, str):
            sys_text = system
        self.calls.append(
            {
                "model": model,
                "tools": tools,
                "tool_choice": tool_choice,
                "thinking": thinking,
                "system_text": sys_text,
            }
        )
        if "haiku" in model:
            return self._haiku
        resp = self._sonnet[min(self._i, len(self._sonnet) - 1)]
        self._i += 1
        return resp


class StubClient:
    """Drop-in for anthropic.Anthropic in tests. Branches sonnet/haiku by model name."""

    def __init__(self, sonnet_script: list[StubResponse], haiku_text: str) -> None:
        self.messages = _StubMessages(sonnet_script, haiku_text)

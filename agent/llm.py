"""LLM client helpers — prompt loading + real-client factory.

Prompts live in versioned files under agent/prompts/, never inline (CLAUDE.md §14).
load_prompt strips the leading HTML-comment metadata block so the model only sees
the prompt body, while the version stays recorded in the run trace / Claim.extracted_by.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Combined thinking + text budget for a single model response. Fine for short
# tool calls; prose-only synthesis uses create_text, which disables thinking so
# the full budget is available for the brief itself.
MAX_TOKENS = 4096

# Long-form prose budget for the synthesis + grounding-retry calls (create_text).
# The v2.2 brief (so-what box + per-winner "why it persists" clauses + the three-way
# distinction in What Changed Recently) is longer than the v2.1 brief; 4096 truncated
# it mid-section (stop_reason="max_tokens" silently dropped Rippling-relevance +
# Unverified signals). 8192 gives headroom; thinking is disabled in create_text so the
# whole budget is text. Output tokens are cheap and this is one call per run.
SYNTHESIS_MAX_TOKENS = 8192


def text_of(resp: Any) -> str:
    """First text block of a model response, or '' when none (e.g. a tool_use-only reply)."""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def tool_uses_of(resp: Any) -> list[Any]:
    return [b for b in resp.content if getattr(b, "type", None) == "tool_use"]


def create(client: Any, model: str, system: Any, messages: list, tools: list[dict]) -> Any:
    """Model call with tools offered — the orchestrator's plan/act loop."""
    return client.messages.create(
        model=model, max_tokens=MAX_TOKENS, system=system, messages=messages, tools=tools
    )


def create_text(
    client: Any,
    model: str,
    system: Any,
    messages: list,
    max_tokens: int = MAX_TOKENS,
    tools: list[dict] | None = None,
) -> Any:
    """Prose-only call: tool use forbidden, extended thinking disabled.

    Two ways to forbid tools, chosen by whether cache reuse matters:
    - ``tools=None`` (default): send ``tools=[]`` — the original behavior.
    - ``tools=<cached schemas>``: send the schemas with ``tool_choice={"type": "none"}``.
      The API guarantees no tool_use blocks, and because tools precede system in the
      prompt-cache key, the call now READS the tools+system cache entry the tool cycle
      already wrote instead of writing a separate system-only entry. Synthesis,
      grounding retries, and the follow-up forced answer pass their cached schemas.

    ``thinking={"type":"disabled"}`` remains the load-bearing part (see git history:
    thinking otherwise consumes the combined token budget and the brief comes back empty).

    ``max_tokens`` defaults to MAX_TOKENS (tool-call-sized); callers writing the full
    brief pass SYNTHESIS_MAX_TOKENS so a long v2.2 brief does not truncate mid-section.
    """
    kwargs: dict[str, Any] = (
        {"tools": tools, "tool_choice": {"type": "none"}} if tools else {"tools": []}
    )
    return client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        thinking={"type": "disabled"},
        **kwargs,
    )


def load_prompt(name: str) -> str:
    """Read agent/prompts/{name}.md and strip the leading <!-- ... --> metadata block."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    if text.lstrip().startswith("<!--"):
        end = text.find("-->")
        if end != -1:
            text = text[end + 3 :].lstrip()
    return text


def make_client(api_key: str, base_url: str | None = None) -> Any:
    """Create the real Anthropic client. Lazy import so no-key tests don't require it.

    Pins base_url to https://api.anthropic.com by default. The SDK would otherwise
    inherit ANTHROPIC_BASE_URL from the shell — which in a Claude-Code-via-Fireworks
    session points at Fireworks and 401s a real Anthropic key (the key was always valid;
    the endpoint was wrong). Override only with an explicit project env var
    (INTEL_ANTHROPIC_BASE_URL) when a proxy is intentionally used.
    """
    import anthropic  # local import: keeps the module importable without the SDK installed

    if base_url is None:
        base_url = os.environ.get("INTEL_ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    return anthropic.Anthropic(api_key=api_key, base_url=base_url)

"""Bulk claim extraction worker — Claude Haiku (the DRY(E) extraction step).

Eng review D7: batch several pages per Haiku call. The excerpt passed in already
carries ### SOURCE: <url> headers (produced by crawl_site / meta_ads), so per-claim
source_url attribution survives batching. Returns raw claim dicts; the loop builds
Claim objects and computes confidence with the deterministic rubric.
"""

from __future__ import annotations

import json
from typing import Any

from agent.cost import Usage

EXTRACTOR_MODEL = "claude-haiku-4-5"
EXTRACTOR_PROMPT_VERSION = "extractor_v3"


def extract_claims_sync(excerpt: str, client: Any, prompt: str) -> tuple[list[dict], Usage]:
    """Synchronous extraction (Haiku is fast; the loop may run it in a thread).

    Returns (raw_claims, usage). Each raw claim is a dict with keys:
    statement, category, source_url, excerpt, and (ad claims only, optional)
    first_seen/last_seen ISO date strings copied verbatim by the extractor.
    """
    resp = client.messages.create(
        model=EXTRACTOR_MODEL,
        max_tokens=4096,
        system=prompt,
        messages=[{"role": "user", "content": excerpt}],
    )
    usage = Usage.from_sdk(resp.usage)
    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            break
    return _parse_claims(text), usage


def _parse_claims(text: str) -> list[dict]:
    """Pull the JSON array out of the model response (it may have surrounding prose)."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [c for c in data if isinstance(c, dict) and c.get("source_url")]

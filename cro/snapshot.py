"""Fetch one landing page and parse it into addressable copy blocks (the control arm).

Split deliberately in two:
  PageSnapshotTool  — subclasses BaseTool, returns SourceResult | ToolFailure like every
                      other tool. Network lives here and nowhere else.
  parse_snapshot()  — PURE. markdown -> PageSnapshot, no I/O, no LLM.

The parse is deterministic markdown structure, not an extraction prompt: a landing page's
h1 IS the hero, and the first paragraph after it IS the subhead. Spending a Haiku call to
rediscover that would be paying tokens for something the document structure already states
(DRY(E)). It also means the parser is testable offline for free, which the eval suite needs.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from cro.models import PageElement, PageSnapshot
from ledger.models import SourceResult, ToolFailure
from tools._auth import bearer
from tools._base import BaseTool
from tools._transport import HttpRequest

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"

# Link text that reads as a call to action rather than navigation.
_CTA_PATTERNS = (
    "get started",
    "start free",
    "try free",
    "try it",
    "book a demo",
    "request a demo",
    "see a demo",
    "watch demo",
    "talk to sales",
    "contact sales",
    "sign up",
    "get a quote",
    "see pricing",
    "learn more",
)
# Nav/legal/footer link text that must never be mistaken for the primary CTA.
_NAV_NOISE = ("login", "log in", "sign in", "privacy", "terms", "careers", "support", "blog")

_H1 = re.compile(r"^#\s+(.+?)\s*$", re.M)
_MD_LINK = re.compile(r"\[([^\]]{1,60})\]\(([^)]+)\)")
_PROOF_TOKEN = re.compile(r"\d")
_PROOF_WORDS = ("customer", "business", "companies", "rated", "review", "trusted", "save", "%")


class PageSnapshotArgs(BaseModel):
    url: str = Field(description="Full URL of the landing page to snapshot")


class PageSnapshotTool(BaseTool):
    name = "page_snapshot"
    description = "Fetch a single landing page as markdown for CRO variant generation."
    args_schema = PageSnapshotArgs
    required_env = ["FIRECRAWL_API_KEY"]

    async def run(self, url: str) -> SourceResult | ToolFailure:
        target = url if url.startswith("http") else f"https://{url}"
        result = await self._request(
            HttpRequest(
                "POST",
                f"{FIRECRAWL_BASE}/scrape",
                json_body={"url": target, "formats": ["markdown"]},
                headers=bearer("FIRECRAWL_API_KEY"),
            )
        )
        if isinstance(result, ToolFailure):
            return result
        try:
            markdown = json.loads(result).get("data", {}).get("markdown", "")
        except (json.JSONDecodeError, AttributeError):
            return self._failure(
                "Firecrawl /scrape returned non-JSON", suggestion="retry or pick another page"
            )
        return SourceResult(
            source=self.name,
            url=target,
            fetched_at=datetime.now(UTC),
            raw_excerpt=markdown,
            status="ok" if markdown.strip() else "empty",
        )


def parse_snapshot(url: str, markdown: str, fetched_at: datetime | None = None) -> PageSnapshot:
    """markdown -> PageSnapshot. Pure, deterministic, no network.

    Raises ValueError when no hero can be found: a page with no h1 has no control arm,
    and generating variants against nothing is worse than failing loudly.
    """
    hero = _first_h1(markdown)
    if not hero:
        raise ValueError(f"no h1 found in {url} — cannot establish a control hero")

    elements = [PageElement(role="hero", text=hero, selector_hint="h1")]
    if subhead := _subhead_after(markdown, hero):
        elements.append(PageElement(role="subhead", text=subhead, selector_hint="h1 + p"))
    if cta := _primary_cta(markdown):
        elements.append(PageElement(role="cta", text=cta, selector_hint="a[href]"))
    if proof := _proof_line(markdown):
        elements.append(PageElement(role="proof", text=proof, selector_hint=""))

    return PageSnapshot(
        url=url,
        fetched_at=fetched_at or datetime.now(UTC),
        elements=elements,
        raw_excerpt=markdown[:4000],
    )


def _first_h1(markdown: str) -> str:
    match = _H1.search(markdown)
    return _strip_md(match.group(1)) if match else ""


def _subhead_after(markdown: str, hero: str) -> str:
    """First substantive prose line after the h1 — not a heading, link, or bullet."""
    lines = markdown.splitlines()
    start = next((i for i, ln in enumerate(lines) if hero in _strip_md(ln)), -1)
    if start < 0:
        return ""
    for line in lines[start + 1 : start + 12]:
        text = _strip_md(line)
        if not text or line.lstrip().startswith(("#", "-", "*", ">", "|")):
            continue
        if len(text.split()) >= 4:
            return text
    return ""


def _primary_cta(markdown: str) -> str:
    """First link whose text reads as a CTA. Nav/legal noise is excluded outright."""
    for text, _href in _MD_LINK.findall(markdown):
        candidate = _strip_md(text)
        low = candidate.lower()
        if any(noise in low for noise in _NAV_NOISE):
            continue
        if any(pattern in low for pattern in _CTA_PATTERNS):
            return candidate
    return ""


def _proof_line(markdown: str) -> str:
    """A line carrying a number plus a proof word — the page's existing social proof."""
    for line in markdown.splitlines():
        text = _strip_md(line)
        if not text or len(text.split()) > 20:
            continue
        low = text.lower()
        if _PROOF_TOKEN.search(text) and any(w in low for w in _PROOF_WORDS):
            return text
    return ""


def _strip_md(text: str) -> str:
    """Drop markdown syntax so comparisons work on prose, not punctuation."""
    text = _MD_LINK.sub(r"\1", text)
    text = re.sub(r"[#*_`>]+", "", text)
    return text.strip()

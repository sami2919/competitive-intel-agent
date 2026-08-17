"""crawl_site — Firecrawl site crawl (homepage, /pricing, product + blog pages).

Eng review D1: routes through the shared transport (no per-vendor SDK). D6: extracts
UTM params from discovered links. Firecrawl strategy: /map first (1 credit), then
/scrape only the priority pages (avoid stealth mode = 5x credits; avoid /extract —
we do our own extraction with Haiku).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ledger.models import SourceResult, ToolFailure
from tools._auth import bearer
from tools._base import BaseTool
from tools._transport import HttpRequest

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"


class CrawlSiteArgs(BaseModel):
    domain: str = Field(description="Competitor domain, e.g. gusto.com")
    max_pages: int = Field(default=12, description="Max pages to scrape")


class CrawlSiteTool(BaseTool):
    name = "crawl_site"
    description = (
        "Crawl a competitor's public website via Firecrawl — homepage, /pricing, "
        "product pages, recent blog posts. Returns concatenated page markdown with "
        "### SOURCE: <url> headers, ready for claim extraction."
    )
    args_schema = CrawlSiteArgs
    required_env = ["FIRECRAWL_API_KEY"]

    async def run(self, domain: str, max_pages: int = 12) -> SourceResult | ToolFailure:
        base = domain if domain.startswith("http") else f"https://{domain}"
        auth = bearer("FIRECRAWL_API_KEY")

        map_result = await self._request(
            HttpRequest("POST", f"{FIRECRAWL_BASE}/map", json_body={"url": base}, headers=auth)
        )
        if isinstance(map_result, ToolFailure):
            return map_result
        try:
            urls: list[str] = json.loads(map_result).get("links", [])
        except (json.JSONDecodeError, AttributeError):
            return self._failure("Firecrawl /map returned non-JSON")

        picked = self._pick_urls(base, urls, max_pages)
        pages: list[tuple[str, str]] = []
        for u in picked:
            sc = await self._request(
                HttpRequest(
                    "POST",
                    f"{FIRECRAWL_BASE}/scrape",
                    json_body={"url": u, "formats": ["markdown"]},
                    headers=auth,
                )
            )
            if isinstance(sc, ToolFailure):
                continue  # skip-empty: a failed page is noted + skipped, not fatal
            try:
                md = json.loads(sc).get("data", {}).get("markdown", "")
            except (json.JSONDecodeError, AttributeError):
                continue
            if md:
                pages.append((u, md))

        if not pages:
            return SourceResult(
                source=self.name,
                url=base,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )
        excerpt = "\n\n".join(f"### SOURCE: {u}\n{md}" for u, md in pages)
        return SourceResult(
            source=self.name,
            url=base,
            fetched_at=datetime.now(UTC),
            raw_excerpt=excerpt,
            status="ok",
        )

    @staticmethod
    def _pick_urls(base: str, urls: list[str], max_pages: int) -> list[str]:
        """Priority: homepage, /pricing, then product/blog pages up to max_pages."""
        normalized = [u if u.startswith("http") else f"https://{u}" for u in urls]
        picked: list[str] = []
        # homepage first
        for u in normalized:
            if u.rstrip("/") == base.rstrip("/") and u not in picked:
                picked.append(u)
        # /pricing next
        for u in normalized:
            if "/pricing" in u and u not in picked:
                picked.append(u)
        # then everything else useful (skip nav fragments / anchors)
        for u in normalized:
            if "#" in u or u.endswith(".pdf") or u in picked:
                continue
            picked.append(u)
            if len(picked) >= max_pages:
                break
        return picked[:max_pages]

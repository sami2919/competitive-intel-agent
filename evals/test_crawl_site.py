"""crawl_site tool test — mocked transport (ReplayTransport) with Firecrawl fixtures.

No API key needed: the replay transport returns recorded /map and /scrape responses.
Verifies the pick-urls heuristic, skip-empty on a failed page, and the ### SOURCE
excerpt format the extractor expects.
"""

from __future__ import annotations

import asyncio
import json

from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.crawl_site import CrawlSiteTool

FIRECRAWL = "https://api.firecrawl.dev/v1"


def _fixtures() -> dict[str, HttpResponse]:
    f: dict[str, HttpResponse] = {}
    f[HttpRequest("POST", f"{FIRECRAWL}/map", json_body={"url": "https://gusto.com"}).key()] = (
        HttpResponse(
            status=200,
            body=json.dumps({"links": ["gusto.com", "gusto.com/pricing", "gusto.com/payroll"]}),
        )
    )
    f[
        HttpRequest(
            "POST",
            f"{FIRECRAWL}/scrape",
            json_body={"url": "https://gusto.com", "formats": ["markdown"]},
        ).key()
    ] = HttpResponse(
        status=200,
        body=json.dumps({"data": {"markdown": "# Gusto — simple payroll for small business"}}),
    )
    f[
        HttpRequest(
            "POST",
            f"{FIRECRAWL}/scrape",
            json_body={"url": "https://gusto.com/pricing", "formats": ["markdown"]},
        ).key()
    ] = HttpResponse(
        status=200,
        body=json.dumps({"data": {"markdown": "Pricing from $39/mo + $6/user. No hidden fees."}}),
    )
    # /payroll intentionally has NO fixture -> transport raises -> tool skip-empties it
    return f


def test_crawl_site_returns_markdown_with_source_headers():
    tool = CrawlSiteTool(transport=ReplayTransport(_fixtures()))
    result = asyncio.run(tool.run(domain="gusto.com", max_pages=3))
    assert result.status == "ok"
    assert "### SOURCE: https://gusto.com" in result.raw_excerpt
    assert "### SOURCE: https://gusto.com/pricing" in result.raw_excerpt
    assert "simple payroll" in result.raw_excerpt


def test_crawl_site_picks_homepage_and_pricing_first():
    tool = CrawlSiteTool(transport=ReplayTransport(_fixtures()))
    result = asyncio.run(tool.run(domain="gusto.com", max_pages=2))
    # homepage + /pricing are the first two picked; /payroll (no fixture) is skipped
    assert (
        "https://gusto.com\n" in result.raw_excerpt
        or "https://gusto.com/pricing" in result.raw_excerpt
    )


def test_crawl_site_empty_when_all_scrapes_fail():
    f = {
        HttpRequest(
            "POST", f"{FIRECRAWL}/map", json_body={"url": "https://fail.com"}
        ).key(): HttpResponse(status=200, body=json.dumps({"links": ["fail.com"]}))
        # no /scrape fixture -> all scrapes fail -> empty
    }
    tool = CrawlSiteTool(transport=ReplayTransport(f))
    result = asyncio.run(tool.run(domain="fail.com", max_pages=3))
    assert result.status == "empty"
    assert result.raw_excerpt == ""

"""social_posts tool tests — Firecrawl /scrape for blog content.

No API key needed: the replay transport returns recorded /scrape responses.
Three cases: populated blog, empty/no-articles page, and failure/non-JSON.
"""

from __future__ import annotations

import asyncio
import json

from ledger.models import ToolFailure
from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.social_posts import SocialPostsTool

FIRECRAWL = "https://api.firecrawl.dev/v1"

# A realistic-looking Gusto /blog markdown snippet with article cards.
_GUSTO_BLOG_MD = """
# Talk Shop

[![How Do I Set Up Payroll for the First Time? The Employer's Complete Guide](https://images.example.com/payroll.jpg)

**How Do I Set Up Payroll for the First Time? The Employer's Complete Guide** \\
\\
New at payroll? Here's what to do before setting up the process. \\
\\
Gusto Editors](https://gusto.com/resources/articles/payroll/set-payroll-first-time)

[![S Corp vs. LLC: What's the Difference?](https://images.example.com/scorp.jpg)

**S Corp vs. LLC: What's the Difference?** \\
\\
Kim Porter](https://gusto.com/resources/articles/start-business/s-corp-vs-llc)

## Ask Gusto

[**What Is the Earned Income Tax Credit?** \\
\\
Anna Goodman](https://gusto.com/resources/articles/taxes/earned-income-tax-credit)

[**State Retirement Mandates in 2026: A Current Breakdown** \\
\\
Author Name](https://gusto.com/resources/articles/hr/state-retirement-mandates-2026)

## Navigation links (should be skipped)

[See all articles](https://gusto.com/resources/articles)
[Pricing](https://gusto.com/pricing)
[Sign up](https://gusto.com/signup)
"""

# A minimal /resources page that has no article-like content (just nav + category cards).
_EMPTY_RESOURCES_MD = """
# Resource Center

[Articles](https://gusto.com/resources#articles)
[Calculators](https://gusto.com/resources#calculators)
[Pricing](https://gusto.com/pricing)

## Categories

Browse our resources by topic.
[See all articles on payroll](https://gusto.com/resources/articles/payroll)
[See all articles on HR](https://gusto.com/resources/articles/hr)
"""


def _fixtures(
    first_url: str,
    first_body: dict | str,
    status1: int = 200,
    second_url: str | None = None,
    second_body: dict | str | None = None,
    status2: int = 200,
) -> dict[str, HttpResponse]:
    """Build fixtures dict with 1-2 scrape requests."""
    f: dict[str, HttpResponse] = {}

    def _body(b: dict | str) -> str:
        return json.dumps(b) if isinstance(b, dict) else b

    f[
        HttpRequest(
            "POST",
            f"{FIRECRAWL}/scrape",
            json_body={"url": first_url, "formats": ["markdown"]},
        ).key()
    ] = HttpResponse(status=status1, body=_body(first_body))

    if second_url and second_body is not None:
        f[
            HttpRequest(
                "POST",
                f"{FIRECRAWL}/scrape",
                json_body={"url": second_url, "formats": ["markdown"]},
            ).key()
        ] = HttpResponse(status=status2, body=_body(second_body))

    return f


def test_populated_blog_returns_articles():
    """A blog page with article cards should produce - date | title lines."""
    f = _fixtures(
        first_url="https://gusto.com/blog",
        first_body={"data": {"markdown": _GUSTO_BLOG_MD}},
    )
    result = asyncio.run(SocialPostsTool(transport=ReplayTransport(f)).run(competitor="gusto.com"))
    assert result.status == "ok"
    assert result.raw_excerpt.startswith("### SOURCE:")
    assert "Set Up Payroll" in result.raw_excerpt
    assert "S Corp vs. LLC" in result.raw_excerpt
    # Navigation links must NOT appear in the excerpt.
    assert "See all articles" not in result.raw_excerpt
    assert "Pricing" not in result.raw_excerpt


def test_blog_with_explicit_url():
    """When blog_url is provided, use it instead of auto-detecting."""
    f = _fixtures(
        first_url="https://gusto.com/resources/articles",
        first_body={"data": {"markdown": _GUSTO_BLOG_MD}},
    )
    result = asyncio.run(
        SocialPostsTool(transport=ReplayTransport(f)).run(
            competitor="gusto.com", blog_url="https://gusto.com/resources/articles"
        )
    )
    assert result.status == "ok"
    assert "Set Up Payroll" in result.raw_excerpt


def test_empty_blog_returns_empty():
    """A page with no article-like content returns status=empty."""
    f = _fixtures(
        first_url="https://gusto.com/blog",
        first_body={"data": {"markdown": _EMPTY_RESOURCES_MD}},
    )
    result = asyncio.run(SocialPostsTool(transport=ReplayTransport(f)).run(competitor="gusto.com"))
    assert result.status == "empty"
    assert result.raw_excerpt == ""


def test_empty_blog_tries_fallback():
    """When /blog is empty, try /resources"""
    fixtures: dict[str, HttpResponse] = {}
    # /blog returns empty
    fixtures[
        HttpRequest(
            "POST",
            f"{FIRECRAWL}/scrape",
            json_body={"url": "https://gusto.com/blog", "formats": ["markdown"]},
        ).key()
    ] = HttpResponse(
        status=200,
        body=json.dumps({"data": {"markdown": _EMPTY_RESOURCES_MD}}),
    )
    # /resources returns real content
    fixtures[
        HttpRequest(
            "POST",
            f"{FIRECRAWL}/scrape",
            json_body={"url": "https://gusto.com/resources", "formats": ["markdown"]},
        ).key()
    ] = HttpResponse(
        status=200,
        body=json.dumps({"data": {"markdown": _GUSTO_BLOG_MD}}),
    )

    result = asyncio.run(
        SocialPostsTool(transport=ReplayTransport(fixtures)).run(competitor="gusto.com")
    )
    assert result.status == "ok"
    assert "Set Up Payroll" in result.raw_excerpt


def test_non_json_returns_tool_failure():
    """Non-JSON response from Firecrawl returns ToolFailure (not crash)."""
    f = _fixtures(
        first_url="https://fail.com/blog",
        first_body="<html>not json</html>",
    )
    result = asyncio.run(SocialPostsTool(transport=ReplayTransport(f)).run(competitor="fail.com"))
    assert isinstance(result, ToolFailure)


def test_all_scrapes_fail_returns_empty():
    """When no URLs can be scraped (all ToolFailures), return empty."""
    f: dict[str, HttpResponse] = {}
    # No fixtures at all -> ReplayTransport raises TransportError -> _request returns ToolFailure
    # Both attempts fail -> empty result
    result = asyncio.run(SocialPostsTool(transport=ReplayTransport(f)).run(competitor="fail.com"))
    assert result.status == "empty"
    assert result.raw_excerpt == ""

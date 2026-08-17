"""jobs_signals tool tests — Firecrawl /scrape of competitor careers pages.

Pattern matches test_meta_ads.py conventions:
  - ReplayTransport fixtures keyed by HttpRequest(...).key()
  - Populated → status='ok' with '### SOURCE:' header
  - Empty → status='empty', raw_excerpt=''
  - Non-JSON → ToolFailure
  - No real env vars (auth headers = {} when unset)
"""

from __future__ import annotations

import asyncio
import json

from ledger.models import ToolFailure
from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.jobs_signals import JobsSignalsTool

FC = "https://api.firecrawl.dev/v1"


def _scrape_fixture(url: str, markdown: str) -> HttpResponse:
    """Build a Firecrawl /scrape response with markdown content."""
    body = json.dumps({"success": True, "data": {"markdown": markdown, "metadata": {}}})
    return HttpResponse(status=200, body=body)


def _scrape_fixture_html(url: str) -> HttpResponse:
    """Build a Firecrawl /scrape response with HTML (non-parseable markdown)."""
    body = json.dumps({"success": True, "data": {"markdown": "", "metadata": {}}})
    return HttpResponse(status=200, body=body)


def test_populated_careers_page():
    """A careers page with real job listings returns status='ok' and a SOURCE header."""
    markdown = """
# Careers at Gusto
## Open roles
### Software Engineer
### Product Marketing Manager
### Enterprise Account Executive
### Sales Development Representative
### People Operations
""".strip()
    url = "https://gusto.com/careers"
    f = {
        HttpRequest(
            "POST",
            f"{FC}/scrape",
            json_body={"url": url, "formats": ["markdown"]},
        ).key(): _scrape_fixture(url, markdown)
    }
    result = asyncio.run(JobsSignalsTool(transport=ReplayTransport(f)).run(domain="gusto.com"))
    assert result.status == "ok"
    assert "### SOURCE: https://gusto.com/careers" in result.raw_excerpt
    assert "Product Marketing Manager" in result.raw_excerpt
    assert "Enterprise Account Executive" in result.raw_excerpt


def test_fallback_to_jobs():
    """When /careers returns empty but /jobs has content, fallback succeeds."""
    careers_url = "https://gusto.com/careers"
    jobs_url = "https://gusto.com/jobs"
    jobs_md = """
# Jobs at Gusto
## Open roles
### Senior Product Manager
### Growth Marketing Lead
""".strip()
    f = {
        HttpRequest(
            "POST",
            f"{FC}/scrape",
            json_body={"url": careers_url, "formats": ["markdown"]},
        ).key(): _scrape_fixture_html(careers_url),
        HttpRequest(
            "POST",
            f"{FC}/scrape",
            json_body={"url": jobs_url, "formats": ["markdown"]},
        ).key(): _scrape_fixture(jobs_url, jobs_md),
    }
    result = asyncio.run(JobsSignalsTool(transport=ReplayTransport(f)).run(domain="gusto.com"))
    assert result.status == "ok"
    assert "### SOURCE: https://gusto.com/jobs" in result.raw_excerpt
    assert "Growth Marketing Lead" in result.raw_excerpt


def test_empty_careers_and_jobs():
    """Both /careers and /jobs return no content → status='empty'."""
    careers_url = "https://gusto.com/careers"
    jobs_url = "https://gusto.com/jobs"
    f = {
        HttpRequest(
            "POST",
            f"{FC}/scrape",
            json_body={"url": careers_url, "formats": ["markdown"]},
        ).key(): _scrape_fixture(careers_url, ""),
        HttpRequest(
            "POST",
            f"{FC}/scrape",
            json_body={"url": jobs_url, "formats": ["markdown"]},
        ).key(): _scrape_fixture(jobs_url, ""),
    }
    result = asyncio.run(JobsSignalsTool(transport=ReplayTransport(f)).run(domain="gusto.com"))
    assert result.status == "empty"
    assert result.raw_excerpt == ""


def test_explicit_careers_url():
    """An explicit careers_url is used directly without fallback."""
    md = "# Deel Careers\n## Open roles\n### Enterprise AE\n### Marketing Director"
    url = "https://deel.com/jobs"
    f = {
        HttpRequest(
            "POST",
            f"{FC}/scrape",
            json_body={"url": url, "formats": ["markdown"]},
        ).key(): _scrape_fixture(url, md)
    }
    result = asyncio.run(
        JobsSignalsTool(transport=ReplayTransport(f)).run(domain="deel.com", careers_url=url)
    )
    assert result.status == "ok"
    assert "Deel Careers" in result.raw_excerpt
    assert "Enterprise AE" in result.raw_excerpt


def test_non_json_returns_tool_failure():
    """A non-JSON response from Firecrawl produces a ToolFailure."""
    f = {
        HttpRequest(
            "POST",
            f"{FC}/scrape",
            json_body={"url": "https://x.com/careers", "formats": ["markdown"]},
        ).key(): HttpResponse(status=200, body="<html>error page</html>")
    }
    result = asyncio.run(JobsSignalsTool(transport=ReplayTransport(f)).run(domain="x.com"))
    assert isinstance(result, ToolFailure)


def test_firecrawl_error_propagates():
    """An HTTP error from Firecrawl returns a ToolFailure (not crash)."""
    f = {
        HttpRequest(
            "POST",
            f"{FC}/scrape",
            json_body={"url": "https://fail.com/careers", "formats": ["markdown"]},
        ).key(): HttpResponse(status=500, body="Internal Server Error")
    }
    result = asyncio.run(JobsSignalsTool(transport=ReplayTransport(f)).run(domain="fail.com"))
    assert isinstance(result, ToolFailure)

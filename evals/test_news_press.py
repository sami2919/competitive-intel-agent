"""news_press tool tests — Exa /search for competitor news coverage.

Response shape confirmed by live probe:
  {results: [{id, title, url, publishedDate, highlights: [...], ...}], ...}

Empty results -> status='empty' (legitimate: the competitor simply has no recent
press coverage). Non-JSON / HTTP errors -> ToolFailure.
"""

from __future__ import annotations

import asyncio
import json

from ledger.models import ToolFailure
from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.news_press import NewsPressTool

EXA = "https://api.exa.ai"
SEARCH = f"{EXA}/search"

# Realistic fixture data matching the Exa /search response shape (confirmed by
# live probe: results[].id, title, url, publishedDate, highlights).
FIXTURE_RESULTS = [
    {
        "id": "https://www.prnewswire.com/news-releases/gusto-launches-cofounder-123",
        "title": "Gusto Launches Cofounder, an AI Teammate for Small Business",
        "url": "https://www.prnewswire.com/news-releases/gusto-launches-cofounder-123",
        "publishedDate": "2026-06-02T14:30:00.000Z",
        "author": "Gusto, Inc.",
        "highlights": [
            "Cofounder is one of the first agentic interfaces for small businesses",
            "Gusto, the leading partner for small businesses, today announced",
        ],
    },
    {
        "id": "https://techcrunch.com/2026/05/20/gusto-funding-round",
        "title": "Gusto Raises $200M Series H at $12B Valuation",
        "url": "https://techcrunch.com/2026/05/20/gusto-funding-round",
        "publishedDate": "2026-05-20T12:00:00.000Z",
        "author": "TechCrunch",
        "highlights": [
            "Gusto has raised $200M in Series H funding",
            "The payroll giant is now valued at $12 billion",
        ],
    },
]


def _search_body(query: str, num_results: int = 10) -> dict:
    return {
        "query": query,
        "type": "auto",
        "category": "news",
        "numResults": num_results,
        "contents": {"highlights": True},
    }


def _fixture(body: dict, response_body: dict) -> dict[str, HttpResponse]:
    return {
        HttpRequest("POST", SEARCH, json_body=body).key(): HttpResponse(
            status=200, body=json.dumps(response_body)
        )
    }


def test_populated_returns_excerpt_with_source_header():
    body = _search_body("Gusto news announcements", 3)
    f = _fixture(body, {"results": FIXTURE_RESULTS})
    result = asyncio.run(
        NewsPressTool(transport=ReplayTransport(f)).run(competitor="Gusto", num_results=3)
    )
    assert result.status == "ok"
    assert "### SOURCE: exa:Gusto news announcements" in result.raw_excerpt
    assert "Gusto Launches Cofounder" in result.raw_excerpt
    assert "2026-06-02T14:30:00.000Z" in result.raw_excerpt
    assert "Gusto Raises $200M" in result.raw_excerpt
    assert "2026-05-20T12:00:00.000Z" in result.raw_excerpt
    assert "agentic interfaces" in result.raw_excerpt


def test_empty_results_returns_empty():
    body = _search_body("Gusto news announcements", 10)
    f = _fixture(body, {"results": []})
    result = asyncio.run(NewsPressTool(transport=ReplayTransport(f)).run(competitor="Gusto"))
    assert result.status == "empty"
    assert result.raw_excerpt == ""


def test_missing_results_key_returns_empty():
    body = _search_body("Gusto news announcements", 10)
    f = _fixture(body, {"requestId": "abc-123"})
    result = asyncio.run(NewsPressTool(transport=ReplayTransport(f)).run(competitor="Gusto"))
    assert result.status == "empty"
    assert result.raw_excerpt == ""


def test_non_json_returns_tool_failure():
    body = _search_body("Gusto news announcements", 10)
    f = {
        HttpRequest("POST", SEARCH, json_body=body).key(): HttpResponse(
            status=200, body="<html>not json</html>"
        )
    }
    result = asyncio.run(NewsPressTool(transport=ReplayTransport(f)).run(competitor="Gusto"))
    assert isinstance(result, ToolFailure)


def test_http_error_returns_tool_failure():
    body = _search_body("Gusto news announcements", 10)
    f = {
        HttpRequest("POST", SEARCH, json_body=body).key(): HttpResponse(
            status=429, body='{"error": "rate limited"}'
        )
    }
    result = asyncio.run(NewsPressTool(transport=ReplayTransport(f)).run(competitor="Gusto"))
    assert isinstance(result, ToolFailure)

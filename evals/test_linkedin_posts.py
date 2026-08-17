"""linkedin_posts tool tests — Apify actor for public LinkedIn company posts.

No API key needed: the replay transport returns recorded actor responses.
Cases: populated posts, empty dataset, HTTP failure, missing-token self-gate.
"""

from __future__ import annotations

import asyncio
import json

from ledger.models import ToolFailure
from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.linkedin_posts import ACTOR_PATH, APIFY_BASE, POSTS_LIMIT, LinkedInPostsTool

# Trimmed from the recorded live probe (Task 1, harvestapi actor, Deel run 2026-07-27).
# Real shape: text in `content`, date nested in `postedAt.date`, url in `linkedinUrl`,
# likes nested in `engagement.likes`.
_ITEMS = [
    {
        "content": "Introducing Gusto Global: hire and pay international contractors in 80+ countries.",
        "postedAt": {"date": "2026-07-14T09:00:00.000Z", "postedAgoShort": "2w"},
        "linkedinUrl": "https://www.linkedin.com/posts/gusto_global-activity-1",
        "engagement": {"likes": 214, "comments": 12, "shares": 3},
        "author": {"name": "Gusto", "type": "company"},
        "type": "post",
    },
    {
        "content": "Small business payroll shouldn't take all day. See how owners save 5 hours a week.",
        "postedAt": {"date": "2026-07-08T15:30:00.000Z", "postedAgoShort": "3w"},
        "linkedinUrl": "https://www.linkedin.com/posts/gusto_payroll-activity-2",
        "engagement": {"likes": 98, "comments": 4, "shares": 1},
        "author": {"name": "Gusto", "type": "company"},
        "type": "post",
    },
]


def _req(company_url: str, token_present: bool = True) -> HttpRequest:
    return HttpRequest(
        "POST",
        f"{APIFY_BASE}/acts/{ACTOR_PATH}/run-sync-get-dataset-items",
        params={"token": "test-token"} if token_present else None,
        json_body={"targetUrls": [company_url], "maxPosts": POSTS_LIMIT},
        timeout=120.0,
    )


def _tool(fixtures: dict) -> LinkedInPostsTool:
    return LinkedInPostsTool(transport=ReplayTransport(fixtures))


def test_populated_posts_returns_excerpt(monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "test-token")
    company = "https://www.linkedin.com/company/gusto"
    f = {_req(company).key(): HttpResponse(status=200, body=json.dumps(_ITEMS))}
    result = asyncio.run(_tool(f).run(competitor="gusto.com"))
    assert result.status == "ok"
    assert result.raw_excerpt.startswith("### SOURCE: linkedin:")
    assert "Gusto Global" in result.raw_excerpt
    assert "likes=214" in result.raw_excerpt
    assert "date=2026-07-14" in result.raw_excerpt


def test_explicit_company_url_overrides_slug(monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "test-token")
    company = "https://www.linkedin.com/company/gusto-hq"
    f = {_req(company).key(): HttpResponse(status=200, body=json.dumps(_ITEMS))}
    result = asyncio.run(_tool(f).run(competitor="gusto.com", company_url=company))
    assert result.status == "ok"
    assert "gusto-hq" in result.url


def test_empty_dataset_is_empty_not_failure(monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "test-token")
    company = "https://www.linkedin.com/company/gusto"
    f = {_req(company).key(): HttpResponse(status=200, body="[]")}
    result = asyncio.run(_tool(f).run(competitor="gusto.com"))
    assert result.status == "empty"
    assert result.raw_excerpt == ""


def test_http_error_returns_tool_failure(monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "test-token")
    company = "https://www.linkedin.com/company/gusto"
    f = {_req(company).key(): HttpResponse(status=402, body="payment required")}
    result = asyncio.run(_tool(f).run(competitor="gusto.com"))
    assert isinstance(result, ToolFailure)
    assert result.tool == "linkedin_posts"


def test_missing_token_self_gates(monkeypatch):
    monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
    monkeypatch.delenv("APIFY_API_KEY", raising=False)
    result = asyncio.run(_tool({}).run(competitor="gusto.com"))
    assert isinstance(result, ToolFailure)
    assert "APIFY" in result.reason

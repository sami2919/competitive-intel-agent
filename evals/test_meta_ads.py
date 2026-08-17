"""meta_ads tool tests — two-step ScrapeCreators flow + the US-ads gotcha as data.

Two-step (verified against the live API during Phase 0 wiring):
  1. GET /search/companies?query=<name>  -> resolve the official page (exact name +
     BLUE_VERIFIED + B2B category tiebreak).
  2. GET /company/ads?pageId=<id>        -> that page's ads (snapshot.body.text copy,
     start_date_string for longevity, targeted_or_reached_countries for regions).

Zero ads / unresolved page -> status='empty' (the orchestrator narrates it; never
ship noisy keyword ads). Populated ads -> an ok excerpt with D5 inputs.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

from ledger.models import ToolFailure
from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.meta_ads import APIFY_ACTOR_ID, APIFY_BASE, APIFY_RESULTS_LIMIT, MetaAdsTool

SC = "https://api.scrapecreators.com/v1"
COMPANIES = f"{SC}/facebook/adLibrary/search/companies"
COMPANY_ADS = f"{SC}/facebook/adLibrary/company/ads"
APIFY = f"{APIFY_BASE}/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"

# The real Gusto payroll company (verified, financial service) — what _pick_page selects.
GUSTO_COMPANY = {
    "page_id": "123",
    "name": "Gusto",
    "category": "Financial Service",
    "verification": "BLUE_VERIFIED",
    "likes": 67660,
}


def _ad(text: str, start: str, regions: list[str]) -> dict:
    return {
        "snapshot": {"body": {"text": text}},
        "start_date_string": start,
        "targeted_or_reached_countries": regions,
        "is_active": True,
    }


def _fixtures(companies_body: dict, ads_body: dict | None = None) -> dict:
    f: dict[str, HttpResponse] = {
        HttpRequest("GET", COMPANIES, params={"query": "gusto"}).key(): HttpResponse(
            status=200, body=json.dumps(companies_body)
        )
    }
    if ads_body is not None:
        f[HttpRequest("GET", COMPANY_ADS, params={"pageId": "123"}).key()] = HttpResponse(
            status=200, body=json.dumps(ads_body)
        )
    return f


def test_zero_ads_returns_empty_not_failure():
    f = _fixtures({"searchResults": [GUSTO_COMPANY]}, {"results": []})
    result = asyncio.run(
        MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="gusto.com")
    )
    assert result.status == "empty"
    assert result.raw_excerpt == ""  # the gotcha: empty is data the orchestrator narrates


def test_populated_ads_excerpt_has_d5_inputs():
    ads = [
        _ad("Run simple payroll in minutes", "2025-01-15", ["US"]),
        _ad("Hire, pay, and onboard", "2026-06-01", ["US", "CA"]),
    ]
    f = _fixtures({"searchResults": [GUSTO_COMPANY]}, {"results": ads})
    result = asyncio.run(
        MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="gusto.com")
    )
    assert result.status == "ok"
    assert "start_date=2025-01-15" in result.raw_excerpt
    assert "regions=['US', 'CA']" in result.raw_excerpt
    assert "Run simple payroll" in result.raw_excerpt


def test_non_json_returns_tool_failure():
    f = {
        HttpRequest("GET", COMPANIES, params={"query": "x"}).key(): HttpResponse(
            status=200, body="<html>not json</html>"
        )
    }
    result = asyncio.run(MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="x.com"))
    assert isinstance(result, ToolFailure)


def test_unresolved_page_returns_empty_not_noise():
    """A keyword that matches only unrelated pages (e.g. Dolce Gusto coffee) must NOT
    ship those ads as the competitor's — return empty rather than garbage."""
    noise = [
        {
            "page_id": "9",
            "name": "NESCAFE Dolce Gusto France",
            "category": "Food & Beverage Company",
            "verification": "BLUE_VERIFIED",
        }
    ]
    f = _fixtures({"searchResults": noise})  # no company/ads fixture — must not be called
    result = asyncio.run(
        MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="gusto.com")
    )
    assert result.status == "empty"
    assert result.raw_excerpt == ""


# ── Apify fallback (fires on ScrapeCreators ToolFailure, e.g. HTTP 402) ────


def _apify_request(name: str = "gusto", key: str = "test-key") -> HttpRequest:
    """The exact Apify sync-run request the tool issues, so fixture keys match."""
    return HttpRequest(
        "POST",
        f"{APIFY}?token={key}",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        json_body={
            "startUrls": [{"url": f"https://www.facebook.com/{name}"}],
            "resultsLimit": APIFY_RESULTS_LIMIT,
        },
        timeout=120.0,
    )


def _sc_companies_402(name: str = "gusto") -> dict:
    """ScrapeCreators /search/companies returning HTTP 402 (credits exhausted)."""
    return {
        HttpRequest("GET", COMPANIES, params={"query": name}).key(): HttpResponse(
            status=402, body=""
        )
    }


def test_scrapecreators_402_falls_back_to_apify() -> None:
    """ScrapeCreators credits exhausted -> Apify fallback serves the ads.

    Apify output uses camelCase fields (startDateFormatted, targetedOrReachedCountries,
    isActive) but the same snapshot.body.text copy path — the generalized readers parse it.
    """
    ads = [
        {
            "snapshot": {"body": {"text": "Run simple payroll in minutes"}},
            "startDateFormatted": "2025-01-15",
            "targetedOrReachedCountries": ["US", "CA"],
            "isActive": True,
        }
    ]
    f = {**_sc_companies_402(), **{_apify_request().key(): HttpResponse(200, json.dumps(ads))}}
    with patch.dict(os.environ, {"APIFY_API_KEY": "test-key"}):
        result = asyncio.run(
            MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="gusto.com")
        )
    assert result.status == "ok"
    assert "start_date=2025-01-15" in result.raw_excerpt
    assert "regions=['US', 'CA']" in result.raw_excerpt
    assert "Run simple payroll" in result.raw_excerpt
    assert "apify fallback" in result.raw_excerpt  # provenance labeled


def test_scrapecreators_402_apify_empty_returns_empty() -> None:
    """If the Apify fallback also finds zero ads, that is a legitimate empty — not a failure."""
    f = {**_sc_companies_402(), **{_apify_request().key(): HttpResponse(200, json.dumps([]))}}
    with patch.dict(os.environ, {"APIFY_API_KEY": "test-key"}):
        result = asyncio.run(
            MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="gusto.com")
        )
    assert result.status == "empty"
    assert result.raw_excerpt == ""


def test_both_scrapecreators_and_apify_fail_returns_combined_failure() -> None:
    """Both paths fail -> ToolFailure carrying the original ScrapeCreators reason, with the
    Apify outcome annotated in the suggestion (honest failure-as-data, not a bare 402)."""
    f = {**_sc_companies_402(), **{_apify_request().key(): HttpResponse(401, body="")}}
    with patch.dict(os.environ, {"APIFY_API_KEY": "test-key"}):
        result = asyncio.run(
            MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="gusto.com")
        )
    assert isinstance(result, ToolFailure)
    assert "402" in result.reason  # original ScrapeCreators failure preserved
    assert "Apify" in result.suggestion  # fallback outcome annotated


def test_no_apify_key_returns_scrapecreators_failure_without_calling_apify() -> None:
    """No Apify key under either name -> the fallback self-gates and returns the ScrapeCreators
    failure. No Apify fixture is provided; if the tool called Apify anyway, ReplayTransport
    would raise and this test would fail — so it also guards the self-gate."""
    f = _sc_companies_402()  # intentionally no Apify fixture
    with patch.dict(os.environ, {"APIFY_API_KEY": "", "APIFY_API_TOKEN": ""}):
        result = asyncio.run(
            MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="gusto.com")
        )
    assert isinstance(result, ToolFailure)
    assert "402" in result.reason


def test_apify_token_alias_accepted() -> None:
    """APIFY_API_TOKEN (Apify's conventional env name) is accepted alongside APIFY_API_KEY.
    With APIFY_API_KEY blanked and only APIFY_API_TOKEN set, the fallback must still fire."""
    ads = [
        {
            "snapshot": {"body": {"text": "Run simple payroll in minutes"}},
            "startDateFormatted": "2025-01-15",
            "targetedOrReachedCountries": ["US"],
            "isActive": True,
        }
    ]
    f = {
        **_sc_companies_402(),
        _apify_request(key="test-token").key(): HttpResponse(200, json.dumps(ads)),
    }
    with patch.dict(os.environ, {"APIFY_API_KEY": "", "APIFY_API_TOKEN": "test-token"}):
        result = asyncio.run(
            MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="gusto.com")
        )
    assert result.status == "ok"
    assert "Run simple payroll" in result.raw_excerpt


def test_scrapecreators_empty_does_not_trigger_apify_fallback() -> None:
    """A genuine zero-ads result (status='empty') is a SourceResult — it must NOT trigger
    the paid Apify fallback. No Apify fixture is provided; calling Apify would raise."""
    f = _fixtures({"searchResults": [GUSTO_COMPANY]}, {"results": []})  # SC empty, no Apify fixture
    with patch.dict(os.environ, {"APIFY_API_KEY": "test-key"}):
        result = asyncio.run(
            MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="gusto.com")
        )
    assert result.status == "empty"
    assert result.raw_excerpt == ""


def test_apify_no_items_rows_return_empty_not_bogus_ok() -> None:
    """The actor returns HTTP 201 with {error: 'no_items', ...} rows when a startUrl has no
    ads (e.g. the derived facebook.com/<name> URL doesn't resolve to an ad-bearing page).
    Those are not real ads — the fallback must return status='empty', never a bogus 'ok'
    with empty ad fields."""
    no_items = [
        {
            "url": "https://www.facebook.com/gusto",
            "error": "no_items",
            "errorDescription": "Empty or private data for provided input",
        }
    ]
    f = {**_sc_companies_402(), **{_apify_request().key(): HttpResponse(201, json.dumps(no_items))}}
    with patch.dict(os.environ, {"APIFY_API_KEY": "test-key"}):
        result = asyncio.run(
            MetaAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="gusto.com")
        )
    assert result.status == "empty"
    assert result.raw_excerpt == ""

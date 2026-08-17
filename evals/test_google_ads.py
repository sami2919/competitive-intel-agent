"""google_ads tool tests — two-step ScrapeCreators flow (list + per-ad detail).

Verified against the live API during Phase 0 wiring:
  1. GET /v1/google/company/ads?domain=<domain>  -> list of ads (format, dates, adUrl).
  2. GET /v1/google/ad?url=<adUrl>               -> full details (regions, headline,
     description from variations).

Zero ads -> status='empty' (the orchestrator narrates it; never ship noisy keyword
matches). Populated ads -> an ok excerpt with dates, regions, and ad copy.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

from ledger.models import ToolFailure
from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.google_ads import (
    SERPAPI_BASE,
    SERPAPI_ENGINE_DETAILS,
    SERPAPI_ENGINE_LIST,
    GoogleAdsTool,
)

SC = "https://api.scrapecreators.com/v1"
COMPANY_ADS = f"{SC}/google/company/ads"
AD_DETAIL = f"{SC}/google/ad"

_GUSTO_DOMAIN = "gusto.com"


def _list_ad(
    creative_id: str,
    fmt: str = "text",
    first: str = "2025-01-15T00:00:00.000Z",
    last: str = "2026-06-01T12:00:00.000Z",
) -> dict:
    return {
        "advertiserId": "AR05137263983937454081",
        "creativeId": creative_id,
        "format": fmt,
        "adUrl": f"https://adstransparency.google.com/advertiser/X/creative/{creative_id}",
        "advertiserName": "GUSTO CALIFORNIA, INC.",
        "domain": "gusto.com",
        "firstShown": first,
        "lastShown": last,
    }


def _ad_detail(
    creative_id: str,
    headline: str = "",
    description: str = "",
    regions: list[str] | None = None,
    fmt: str = "text",
) -> dict:
    regs = [{"regionCode": r, "regionName": r} for r in (regions or ["US"])]
    var = {
        "destinationUrl": "gusto.com",
        "headline": headline,
        "description": description,
        "allText": f"Sponsored {headline} {description}",
    }
    return {
        "success": True,
        "advertiserId": "AR05137263983937454081",
        "creativeId": creative_id,
        "url": f"https://adstransparency.google.com/advertiser/X/creative/{creative_id}",
        "lastShown": "2026-06-01T12:00:00.000Z",
        "format": fmt,
        "creativeRegions": regs,
        "variations": [var],
    }


def _fixtures(list_body: dict, detail_bodies: dict[str, dict] | None = None) -> dict:
    f: dict[str, HttpResponse] = {
        HttpRequest("GET", COMPANY_ADS, params={"domain": _GUSTO_DOMAIN}).key(): HttpResponse(
            status=200, body=json.dumps(list_body)
        )
    }
    if detail_bodies:
        for cid, body in detail_bodies.items():
            ad_url = f"https://adstransparency.google.com/advertiser/X/creative/{cid}"
            f[HttpRequest("GET", AD_DETAIL, params={"url": ad_url}).key()] = HttpResponse(
                status=200, body=json.dumps(body)
            )
    return f


# --- tests ---


def test_zero_ads_returns_empty_not_failure():
    f = _fixtures({"success": True, "ads": []})
    result = asyncio.run(
        GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
    )
    assert result.status == "empty"
    assert result.raw_excerpt == ""


def test_populated_ads_excerpt_has_metadata_and_copy():
    """A single ad with detail fetch returns format, dates, regions, and ad copy."""
    ad = _list_ad("CR001", first="2024-01-01T00:00:00.000Z")
    detail = _ad_detail("CR001", headline="Simple Payroll", description="Run payroll fast")
    f = _fixtures({"success": True, "ads": [ad]}, {"CR001": detail})
    result = asyncio.run(
        GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
    )
    assert result.status == "ok"
    excerpt = result.raw_excerpt
    # Metadata from list endpoint
    assert "first_shown=2024-01-01" in excerpt
    assert "format=text" in excerpt
    # Regions from detail endpoint
    assert "regions=['US']" in excerpt
    # Ad copy
    assert "Simple Payroll" in excerpt
    assert "Run payroll fast" in excerpt


def test_multiple_ads_all_merged():
    """Two ads with different creative details appear in the excerpt."""
    ads = [
        _list_ad("CR001", first="2024-01-01T00:00:00.000Z"),
        _list_ad("CR002", first="2025-06-01T00:00:00.000Z"),
    ]
    details = {
        "CR001": _ad_detail(
            "CR001", headline="Product A", description="Description A", regions=["US"]
        ),
        "CR002": _ad_detail(
            "CR002", headline="Product B", description="Description B", regions=["US", "CA"]
        ),
    }
    f = _fixtures({"success": True, "ads": ads}, details)
    result = asyncio.run(
        GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
    )
    assert result.status == "ok"
    assert "Product A" in result.raw_excerpt
    assert "Product B" in result.raw_excerpt
    assert "regions=['US', 'CA']" in result.raw_excerpt


def test_detail_fetch_failure_still_includes_metadata():
    """When the detail endpoint fails, we still include the list-level metadata."""
    ad = _list_ad("CR_FAIL", first="2024-06-01T00:00:00.000Z")
    # No detail fixture for CR_FAIL -> transport will error
    f = _fixtures({"success": True, "ads": [ad]})
    result = asyncio.run(
        GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
    )
    assert result.status == "ok"
    assert "CR_FAIL" in result.raw_excerpt
    assert "first_shown=2024-06-01" in result.raw_excerpt


def test_non_json_list_returns_tool_failure():
    f = {
        HttpRequest("GET", COMPANY_ADS, params={"domain": "x.com"}).key(): HttpResponse(
            status=200, body="<html>not json</html>"
        )
    }
    result = asyncio.run(GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain="x.com"))
    assert isinstance(result, ToolFailure)


def test_image_ad_no_text_available():
    """Image/video ads have no text copy; excerpt notes the format."""
    ad = _list_ad("CR_IMG", fmt="image", first="2025-01-01T00:00:00.000Z")
    detail = _ad_detail("CR_IMG", fmt="image")  # no headline/description
    f = _fixtures({"success": True, "ads": [ad]}, {"CR_IMG": detail})
    result = asyncio.run(
        GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
    )
    assert result.status == "ok"
    assert "[image ad - no text available]" in result.raw_excerpt


def test_ad_count_respected():
    """Only MAX_DETAIL_ADS ads get detail fetches; additional ads are metadata-only."""
    ads = [_list_ad(f"CR{i:03d}", first=f"2024-01-{i + 1:02d}T00:00:00.000Z") for i in range(15)]
    details = {f"CR{i:03d}": _ad_detail(f"CR{i:03d}", headline=f"Ad {i}") for i in range(5)}
    f = _fixtures({"success": True, "ads": ads}, details)
    result = asyncio.run(
        GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
    )
    assert result.status == "ok"
    # Only the first ad with a detail fixture should have its copy
    assert "Ad 0" in result.raw_excerpt
    # The 6th ad has no detail fixture and no detail was fetched — should still appear
    # with list metadata but no text copy
    assert "CR005" in result.raw_excerpt


# ── SerpApi fallback (fires on ScrapeCreators ToolFailure, e.g. HTTP 402) ───


def _serpapi_list_request(domain: str = _GUSTO_DOMAIN, key: str = "test-key") -> HttpRequest:
    return HttpRequest(
        "GET",
        SERPAPI_BASE,
        params={"engine": SERPAPI_ENGINE_LIST, "text": domain, "api_key": key},
    )


def _serpapi_detail_request(
    advertiser_id: str, creative_id: str, key: str = "test-key"
) -> HttpRequest:
    return HttpRequest(
        "GET",
        SERPAPI_BASE,
        params={
            "engine": SERPAPI_ENGINE_DETAILS,
            "advertiser_id": advertiser_id,
            "creative_id": creative_id,
            "api_key": key,
        },
    )


def _sc_company_402(domain: str = _GUSTO_DOMAIN) -> dict:
    """ScrapeCreators /google/company/ads returning HTTP 402 (credits exhausted)."""
    return {
        HttpRequest("GET", COMPANY_ADS, params={"domain": domain}).key(): HttpResponse(
            status=402, body=""
        )
    }


def _serpapi_list_creative(
    creative_id: str = "CR001",
    target_domain: str = _GUSTO_DOMAIN,
    advertiser: str = "GUSTO CALIFORNIA, INC.",
    first_shown: int = 1737331200,  # 2025-01-20 UTC
    last_shown: int = 1780272000,  # 2026-06-01 UTC
    fmt: str = "text",
) -> dict:
    return {
        "advertiser_id": "AR05137263983937454081",
        "advertiser": advertiser,
        "ad_creative_id": creative_id,
        "format": fmt,
        "target_domain": target_domain,
        "first_shown": first_shown,
        "last_shown": last_shown,
    }


def test_scrapecreators_402_falls_back_to_serpapi() -> None:
    """ScrapeCreators credits exhausted -> SerpApi fallback serves the ads.

    SerpApi list creatives carry unix-epoch dates + target_domain (noise filter). The
    ad_details engine puts regions under ``search_information.regions`` (region_name) and
    does NOT expose ad copy (Google renders text into the creative image) — so a text ad
    degrades to '[text ad - no copy available]' honestly, with regions + dates preserved.
    """
    creative = _serpapi_list_creative()
    detail = {
        "search_information": {
            "format": "text",
            "regions": [{"region": 2840, "region_name": "United States"}],
        },
        "ad_creatives": [{"image": "https://tpc.googlesyndication.com/archive/simgad/x"}],
    }
    f = {
        **_sc_company_402(),
        _serpapi_list_request().key(): HttpResponse(200, json.dumps({"ad_creatives": [creative]})),
        _serpapi_detail_request("AR05137263983937454081", "CR001").key(): HttpResponse(
            200, json.dumps(detail)
        ),
    }
    with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
        result = asyncio.run(
            GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
        )
    assert result.status == "ok"
    excerpt = result.raw_excerpt
    assert "first_shown=2025-01-20" in excerpt  # unix -> ISO date conversion
    assert "last_shown=2026-06-01" in excerpt
    assert "format=text" in excerpt
    assert "regions=['United States']" in excerpt  # from search_information.regions
    assert "[text ad - no copy available]" in excerpt  # SerpApi exposes no ad copy
    assert "serpapi fallback" in excerpt  # provenance labeled


def test_scrapecreators_402_serpapi_no_target_domain_match_returns_empty() -> None:
    """SerpApi text search returns creatives for OTHER advertisers (target_domain mismatch
    + no name match) -> the noise filter drops them all -> empty, not garbage."""
    noise = [
        _serpapi_list_creative(
            creative_id="CRX", target_domain="apple.com", advertiser="Apple Inc."
        ),
    ]
    f = {
        **_sc_company_402(),
        _serpapi_list_request().key(): HttpResponse(200, json.dumps({"ad_creatives": noise})),
    }
    with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
        result = asyncio.run(
            GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
        )
    assert result.status == "empty"
    assert result.raw_excerpt == ""


def test_both_scrapecreators_and_serpapi_fail_returns_combined_failure() -> None:
    """Both paths fail -> ToolFailure carrying the original ScrapeCreators reason, with the
    SerpApi outcome annotated in the suggestion."""
    f = {
        **_sc_company_402(),
        _serpapi_list_request().key(): HttpResponse(401, body=""),
    }
    with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
        result = asyncio.run(
            GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
        )
    assert isinstance(result, ToolFailure)
    assert "402" in result.reason  # original ScrapeCreators failure preserved
    assert "SerpApi" in result.suggestion  # fallback outcome annotated


def test_no_serpapi_key_returns_scrapecreators_failure_without_calling_serpapi() -> None:
    """No SERPAPI_API_KEY -> the fallback self-gates and returns the ScrapeCreators failure.
    No SerpApi fixture is provided; if the tool called SerpApi anyway, ReplayTransport
    would raise and this test would fail — so it also guards the self-gate."""
    f = _sc_company_402()  # intentionally no SerpApi fixture
    with patch.dict(os.environ, {"SERPAPI_API_KEY": ""}):
        result = asyncio.run(
            GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
        )
    assert isinstance(result, ToolFailure)
    assert "402" in result.reason


def test_scrapecreators_empty_does_not_trigger_serpapi_fallback() -> None:
    """A genuine zero-ads result (status='empty') is a SourceResult — it must NOT trigger
    the paid SerpApi fallback. No SerpApi fixture is provided; calling SerpApi would raise."""
    f = _fixtures({"success": True, "ads": []})  # SC empty, no SerpApi fixture
    with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
        result = asyncio.run(
            GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
        )
    assert result.status == "empty"
    assert result.raw_excerpt == ""


def test_serpapi_detail_failure_degrades_to_metadata_only() -> None:
    """A failed per-ad details call must not break the fallback — the ad still appears with
    list metadata (dates, format) and no copy/regions."""
    creative = _serpapi_list_creative()
    f = {
        **_sc_company_402(),
        _serpapi_list_request().key(): HttpResponse(200, json.dumps({"ad_creatives": [creative]})),
        # No detail fixture -> transport errors -> _serpapi_ad_details returns None
    }
    with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
        result = asyncio.run(
            GoogleAdsTool(transport=ReplayTransport(f)).run(advertiser_domain=_GUSTO_DOMAIN)
        )
    assert result.status == "ok"
    assert "CR001" in result.raw_excerpt
    assert "first_shown=2025-01-20" in result.raw_excerpt
    assert "[text ad - no copy available]" in result.raw_excerpt  # no copy, gracefully

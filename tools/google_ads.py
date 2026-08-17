"""google_ads — Google Ads Transparency ads via ScrapeCreators, with a SerpApi fallback.

GOTCHA (like meta_ads's US Meta Ad Library gotcha): Google's official Ads Transparency
API surfaces commercial ads, but the ScrapeCreators list endpoint (/google/company/ads)
only returns metadata (format, dates, creative IDs) — NOT ad copy text. We must do a
second per-ad fetch (/google/ad) for headlines, descriptions, and region targeting.

TWO-STEP (ScrapeCreators, primary):
  1. GET /v1/google/company/ads?domain=<domain>  -> list of ads with basic metadata
     (creativeId, format, firstShown, lastShown, adUrl).
  2. For the first MAX_DETAIL_ADS ads, GET /v1/google/ad?url=<adUrl> -> full details
     (headline, description, creativeRegions). No text body on image/video ads.

Input: advertiser_domain (e.g. 'gusto.com') — domain parameter on the list endpoint
resolves to the Google-verified advertiser directly, no search-then-resolve step needed.

SERPAPI FALLBACK: if ScrapeCreators returns a ToolFailure (e.g. HTTP 402 credits
exhausted), the tool falls back to SerpApi's google_ads_transparency_center engine.
The fallback is OPTIONAL: it self-gates on SERPAPI_API_KEY so live mode still starts
when only ScrapeCreators is keyed. It fires ONLY on ScrapeCreators *failure*, never on
status="empty" — a genuine zero-ads result is not re-queried on a paid API. SerpApi has
no domain-keyed lookup, so the fallback searches by `text=<domain>` (Google's "search by
advertiser or website name"), which returns creatives across MULTIPLE advertisers — we
filter to the competitor's own creatives via the list creative's `target_domain` field
(name-match as a secondary fallback). Like ScrapeCreators, regions need a second per-ad
call (google_ads_transparency_center_ad_details); that step is capped at
SERPAPI_MAX_DETAIL_ADS to bound SerpApi per-search spend. The ad_details engine puts
regions under `search_information.regions` (each {region_name, ...}); ad COPY (headline/
description) is NOT exposed — Google renders text into the creative image and SerpApi
captures only the image URL — so text ads degrade to '[text ad - no copy available]' in
the excerpt. Metadata (dates, format) + regions, honestly; no fabricated copy. A failed
detail call degrades to metadata-only, never breaks the fallback.

Cost: 1 list search + up to 5 detail searches per fallback. Routes through the shared
transport (api_key query param); 30s default timeout is sufficient (SerpApi search is
fast, unlike the Apify sync actor run).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ledger.models import SourceResult, ToolFailure
from tools._auth import x_api_key
from tools._base import BaseTool
from tools._transport import HttpRequest

SCRAPECREATORS_BASE = "https://api.scrapecreators.com/v1"
MAX_DETAIL_ADS = 10  # cap per-ad detail fetches to keep credit usage reasonable

# SerpApi fallback config. Engine: google_ads_transparency_center (list) +
# google_ads_transparency_center_ad_details (per-creative copy/regions). 30s default
# transport timeout is fine — SerpApi search returns in seconds (no long actor run).
SERPAPI_BASE = "https://serpapi.com/search"
SERPAPI_ENGINE_LIST = "google_ads_transparency_center"
SERPAPI_ENGINE_DETAILS = "google_ads_transparency_center_ad_details"
SERPAPI_MAX_DETAIL_ADS = 5  # bounds SerpApi per-search spend (1 list + 5 details/fallback)


class GoogleAdsArgs(BaseModel):
    advertiser_domain: str = Field(description="Competitor domain, e.g. gusto.com")


class GoogleAdsTool(BaseTool):
    name = "google_ads"
    description = (
        "Search the Google Ads Transparency library for a competitor's ads via "
        "ScrapeCreators, with a SerpApi fallback if ScrapeCreators is unavailable "
        "(e.g. credits exhausted). Fetches the advertiser's active ads, then retrieves "
        "full creative details (headlines, descriptions, regions, dates). Returns ad "
        "copy for text ads; image/video ads are noted without text."
    )
    args_schema = GoogleAdsArgs
    required_env = ["SCRAPECREATORS_API_KEY"]  # SerpApi is an optional fallback (self-gated)

    async def run(self, advertiser_domain: str) -> SourceResult | ToolFailure:
        """ScrapeCreators first; on ToolFailure, try the SerpApi fallback.

        A status='empty' result is a SourceResult (legitimate zero ads) and does NOT
        trigger the fallback — we never re-query a paid API to second-guess a genuine
        empty. Only a ToolFailure (HTTP error, non-JSON, credits exhausted) does.
        """
        sc = await self._scrapecreators_flow(advertiser_domain)
        if isinstance(sc, SourceResult):
            return sc
        serp = await self._serpapi_fallback(advertiser_domain)
        if isinstance(serp, SourceResult):
            return serp
        return self._failure(
            sc.reason,
            suggestion=(
                f"Both ScrapeCreators and SerpApi fallback failed (SerpApi: {serp.reason}). "
                f"{sc.suggestion}".strip()
            ),
            retriable=sc.retriable,
        )

    async def _scrapecreators_flow(self, advertiser_domain: str) -> SourceResult | ToolFailure:
        """The two-step ScrapeCreators flow (primary path)."""
        auth = x_api_key("SCRAPECREATORS_API_KEY")
        source_url = f"google-ads:{advertiser_domain}"

        # Step 1 — fetch the list of ads for this domain.
        list_resp = await self._request(
            HttpRequest(
                "GET",
                f"{SCRAPECREATORS_BASE}/google/company/ads",
                params={"domain": advertiser_domain},
                headers=auth,
            )
        )
        if isinstance(list_resp, ToolFailure):
            return list_resp
        try:
            body = json.loads(list_resp)
            ads: list[dict] = body.get("ads") or []
        except (json.JSONDecodeError, AttributeError):
            return self._failure("ScrapeCreators /google/company/ads returned non-JSON")

        if not ads:
            return SourceResult(
                source=self.name,
                url=source_url,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        # Step 2 — fetch details for each ad (up to limit).
        details: list[dict] = []
        for ad in ads[:MAX_DETAIL_ADS]:
            ad_url = ad.get("adUrl")
            if not ad_url:
                details.append(_merge_detail(ad, None))
                continue
            detail_resp = await self._request(
                HttpRequest(
                    "GET",
                    f"{SCRAPECREATORS_BASE}/google/ad",
                    params={"url": ad_url},
                    headers=auth,
                )
            )
            if isinstance(detail_resp, ToolFailure):
                details.append(_merge_detail(ad, None))
                continue
            try:
                detail = json.loads(detail_resp)
            except (json.JSONDecodeError, AttributeError):
                details.append(_merge_detail(ad, None))
                continue
            details.append(_merge_detail(ad, detail))

        lines = [f"### SOURCE: google-ads:{advertiser_domain}"]
        for item in details:
            lines.append(_format_ad_line(item))

        return SourceResult(
            source=self.name,
            url=source_url,
            fetched_at=datetime.now(UTC),
            raw_excerpt="\n".join(lines),
            status="ok",
        )

    async def _serpapi_fallback(self, advertiser_domain: str) -> SourceResult | ToolFailure:
        """SerpApi fallback when ScrapeCreators fails. Self-gates on SERPAPI_API_KEY."""
        key = os.environ.get("SERPAPI_API_KEY", "").strip()
        source_url = f"google-ads:{advertiser_domain}"
        if not key:
            return self._failure("SERPAPI_API_KEY not set; SerpApi fallback unavailable")

        # Step 1 — list creatives via text=<domain> (Google's "search by website name").
        list_resp = await self._request(
            HttpRequest(
                "GET",
                SERPAPI_BASE,
                params={
                    "engine": SERPAPI_ENGINE_LIST,
                    "text": advertiser_domain,
                    "api_key": key,
                },
            )
        )
        if isinstance(list_resp, ToolFailure):
            return list_resp
        try:
            creatives = json.loads(list_resp).get("ad_creatives") or []
        except (json.JSONDecodeError, AttributeError):
            return self._failure("SerpApi google_ads_transparency_center returned non-JSON")

        # Noise filter: text search returns creatives across multiple advertisers; keep
        # only those targeting our domain (the competitor's own ads). Fall back to a
        # name match if target_domain filtering is too strict (no creative targets it).
        domain_l = advertiser_domain.lower()
        name = advertiser_domain.split(".")[0].lower()
        filtered = [c for c in creatives if (c.get("target_domain") or "").lower() == domain_l]
        if not filtered:
            filtered = [c for c in creatives if name in (c.get("advertiser") or "").lower()]
        if not filtered:
            return SourceResult(
                source=self.name,
                url=source_url,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        # Step 2 — per-ad details (capped) for copy + regions. Best-effort: a failed
        # detail fetch degrades to metadata-only, never blocks the whole fallback.
        details: list[dict] = []
        for c in filtered[:SERPAPI_MAX_DETAIL_ADS]:
            detail = await self._serpapi_ad_details(
                c.get("advertiser_id", ""), c.get("ad_creative_id", ""), key
            )
            details.append(_merge_serpapi_detail(c, detail))

        lines = [f"### SOURCE: google-ads:{advertiser_domain} (serpapi fallback)"]
        for item in details:
            lines.append(_format_ad_line(item))
        return SourceResult(
            source=self.name,
            url=source_url,
            fetched_at=datetime.now(UTC),
            raw_excerpt="\n".join(lines),
            status="ok",
        )

    async def _serpapi_ad_details(
        self, advertiser_id: str, creative_id: str, key: str
    ) -> dict | None:
        """Per-creative details (regions, headline, description). None on any failure."""
        if not advertiser_id or not creative_id:
            return None
        resp = await self._request(
            HttpRequest(
                "GET",
                SERPAPI_BASE,
                params={
                    "engine": SERPAPI_ENGINE_DETAILS,
                    "advertiser_id": advertiser_id,
                    "creative_id": creative_id,
                    "api_key": key,
                },
            )
        )
        if isinstance(resp, ToolFailure):
            return None
        try:
            return json.loads(resp)
        except (json.JSONDecodeError, TypeError):
            return None


# --- helpers ---


def _merge_detail(list_ad: dict, detail_ad: dict | None) -> dict:
    """Merge list-level metadata with the detail endpoint's richer data (ScrapeCreators).

    The list endpoint has reliable firstShown/lastShown; the detail endpoint
    has regions and variations (headline/description). Prefer list dates.
    """
    merged: dict = {
        "creativeId": list_ad.get("creativeId", ""),
        "format": list_ad.get("format", ""),
        "firstShown": list_ad.get("firstShown", ""),
        "lastShown": list_ad.get("lastShown", ""),
        "regions": [],
        "headline": "",
        "description": "",
    }
    if detail_ad:
        # Regions from creativeRegions array.
        merged["regions"] = [
            r.get("regionCode", "") for r in (detail_ad.get("creativeRegions") or [])
        ]
        # Text copy from first variation.
        variations = detail_ad.get("variations") or []
        if variations:
            var = variations[0]
            merged["headline"] = (var.get("headline") or "").strip()
            merged["description"] = (var.get("description") or "").strip()
    return merged


def _merge_serpapi_detail(list_creative: dict, detail_ad: dict | None) -> dict:
    """Map a SerpApi list creative (+ optional ad_details) into the _format_ad_line shape.

    SerpApi list creatives use snake_case + unix-epoch dates (first_shown/last_shown).
    The ad_details engine puts regions under ``search_information.regions`` (each
    ``{region, region_name, last_shown}``) — NOT under ``creativeRegions`` like
    ScrapeCreators. Ad copy (headline/description) is NOT exposed by SerpApi: Google
    renders text into the creative image and SerpApi captures only the image URL, so
    headline/description stay empty and text ads degrade to '[text ad - no copy
    available]' in the excerpt — metadata + regions, honestly, no fabricated copy.
    """
    merged: dict = {
        "creativeId": list_creative.get("ad_creative_id", ""),
        "format": list_creative.get("format", ""),
        "firstShown": _unix_to_date(list_creative.get("first_shown")),
        "lastShown": _unix_to_date(list_creative.get("last_shown")),
        "regions": [],
        "headline": "",
        "description": "",
    }
    if detail_ad:
        si = detail_ad.get("search_information") or {}
        regions_raw = si.get("regions") or detail_ad.get("creativeRegions") or []
        merged["regions"] = [
            r.get("region_name", "") or r.get("regionCode", "") if isinstance(r, dict) else str(r)
            for r in regions_raw
        ]
    return merged


def _format_ad_line(item: dict) -> str:
    """One line per ad for the excerpt (shared by ScrapeCreators + SerpApi paths)."""
    fmt = item.get("format", "unknown")
    headline = item.get("headline", "") or ""
    desc = item.get("description", "") or ""
    if fmt == "text" and headline:
        copy_text = f"{headline} — {desc}" if desc else headline
    elif fmt == "text":
        copy_text = desc or "[text ad - no copy available]"
    else:
        copy_text = f"[{fmt} ad - no text available]"

    first = _trim_iso(item.get("firstShown", ""))
    last = _trim_iso(item.get("lastShown", ""))
    regions = item.get("regions", [])

    return (
        f"- creativeId={item.get('creativeId', '')}"
        f" | format={fmt}"
        f" | first_shown={first}"
        f" | last_shown={last}"
        f" | regions={regions}"
        f" | copy: {copy_text[:200]}"
    )


def _trim_iso(ts: str) -> str:
    """2026-07-14T21:23:50.000Z -> 2026-07-14 (or keep empty)."""
    if not ts:
        return ""
    return ts[:10] if len(ts) >= 10 else ts


def _unix_to_date(ts: object) -> str:
    """Unix epoch int -> YYYY-MM-DD. Empty string when absent/unparseable."""
    if ts in (None, "", 0):
        return ""
    try:
        return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""

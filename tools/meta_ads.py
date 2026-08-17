"""meta_ads — Meta Ad Library active ads via ScrapeCreators, with an Apify fallback.

THE GOTCHA (CLAUDE.md §4 / DECISIONS.md centerpiece): the official Meta Ad Library
API returns NOTHING for US commercial ads (political/issue + EU/UK only). We use
ScrapeCreators instead. On zero ads — a legitimate result, not a failure — return
status="empty" so the orchestrator can surface it conversationally ("No active Meta
ads found — check LinkedIn Ad Library instead?"). This is the planted Loom moment.

TWO-STEP (required for data quality): a bare keyword /search/ads matches the WORD
"gusto" (Spanish/Italian for "taste") in ad copy across Amazon México, Nescafé Dolce
Gusto, and Bimbo — NOT the advertiser Gusto. So:
  1. GET /search/companies?query=<name>  -> resolve the competitor's official page
     (exact name match; ties broken by BLUE_VERIFIED + financial/B2B category).
  2. GET /company/ads?pageId=<page_id>   -> that page's ads (real ad copy, start
     dates for longevity, regions, active status).
If the official page can't be resolved, return empty rather than ship noisy keyword
ads — "it's better to skip a prospect than send garbage."

APIFY FALLBACK: if ScrapeCreators returns a ToolFailure (e.g. HTTP 402 — free credits
exhausted), the tool falls back to the `apify/facebook-ads-scraper` actor via Apify's
sync run endpoint. The fallback is OPTIONAL: it self-gates on the Apify key (reads
APIFY_API_KEY, falling back to APIFY_API_TOKEN — Apify's conventional name) so live mode
still starts when only ScrapeCreators is keyed. It fires ONLY on ScrapeCreators *failure*,
never on status="empty" — a genuine zero-ads result is not re-queried on a paid API. The
actor is searched by startUrls (the competitor's Facebook page URL, derived from the
domain); it resolves the page ID itself. Its output mirrors ScrapeCreators closely
(snapshot.body.text, startDateFormatted, targetedOrReachedCountries, isActive), so
_ad_copy and the generalized field readers reuse cleanly.

Best-effort limitation: the derived facebook.com/<name> URL is a heuristic — it works when
the domain matches the competitor's FB vanity URL, but some advertisers' ad-bearing pages
live at a different URL (ScrapeCreators resolves these via name search + page_id, which is
the path that's down). When the actor finds no ads for the derived URL it returns HTTP 201
with {error: "no_items"} rows; those are filtered out and the fallback returns
status="empty" honestly rather than ship a bogus 'ok' with empty fields. So the fallback
recovers ad data when it can, and degrades to a narrated empty when it can't — never garbage.

D5: surfaces ad-confidence inputs (start_date for longevity, region count for
expansion) so the deterministic rubric can score performance inferences. No
variant/refresh-count signal is computed here — refreshed is always False in
build.py's D5 call (documented gap, see DECISIONS.md next steps). D1: routes
through the shared transport (x-api-key header for ScrapeCreators; token query
param + a per-request 120s timeout for the Apify sync actor run).
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

# Apify fallback config. Actor: apify/facebook-ads-scraper (API acts path uses ~).
# Sync run endpoint blocks until the actor finishes — hence the 120s per-request
# timeout (set on HttpRequest.timeout, honored by LiveTransport over the 30s default).
APIFY_BASE = "https://api.apify.com/v2"
APIFY_ACTOR_ID = "apify~facebook-ads-scraper"
APIFY_TIMEOUT_S = 120.0
APIFY_RESULTS_LIMIT = 20  # $0.75/1000 results -> ~$0.015/call; enough for theme sampling

# Categories that mark a real B2B/financial advertiser vs. a coincidental name match
# (a coffee brand, a cooking channel, a clothing store).
_BIZ_CATEGORIES = ("financial", "software", "payroll", "business", "company", "product/service")


class MetaAdsArgs(BaseModel):
    advertiser_domain: str = Field(description="Competitor domain, e.g. gusto.com")


class MetaAdsTool(BaseTool):
    name = "meta_ads"
    description = (
        "Search the Meta Ad Library for a competitor's active ads via ScrapeCreators, "
        "with an Apify fallback if ScrapeCreators is unavailable (e.g. credits exhausted). "
        "Resolves the competitor's official Facebook page first, then fetches that "
        "page's ads. Returns ad copy, start dates (longevity), regions, and active "
        "status. May return zero ads for US commercial targets — that is expected."
    )
    args_schema = MetaAdsArgs
    required_env = ["SCRAPECREATORS_API_KEY"]  # Apify is an optional fallback (self-gated)

    async def run(self, advertiser_domain: str) -> SourceResult | ToolFailure:
        """ScrapeCreators first; on ToolFailure, try the Apify fallback.

        A status='empty' result is a SourceResult (legitimate zero ads) and does NOT
        trigger the fallback — we never re-query a paid API to second-guess a genuine
        empty. Only a ToolFailure (HTTP error, non-JSON, credits exhausted) does.
        """
        sc = await self._scrapecreators_flow(advertiser_domain)
        if isinstance(sc, SourceResult):
            return sc
        apify = await self._apify_fallback(advertiser_domain)
        if isinstance(apify, SourceResult):
            return apify
        # Both paths failed — return the original ScrapeCreators failure, annotated
        # with the Apify outcome so the orchestrator's failure-as-data message is honest.
        return self._failure(
            sc.reason,
            suggestion=(
                f"Both ScrapeCreators and Apify fallback failed (Apify: {apify.reason}). "
                f"{sc.suggestion}".strip()
            ),
            retriable=sc.retriable,
        )

    async def _scrapecreators_flow(self, advertiser_domain: str) -> SourceResult | ToolFailure:
        """The two-step ScrapeCreators flow (primary path)."""
        auth = x_api_key("SCRAPECREATORS_API_KEY")
        name = self._derive_name(advertiser_domain)
        url = f"meta-ad-library:{advertiser_domain}"

        # Step 1 — resolve the competitor's official Facebook page.
        companies = await self._request(
            HttpRequest(
                "GET",
                f"{SCRAPECREATORS_BASE}/facebook/adLibrary/search/companies",
                params={"query": name},
                headers=auth,
            )
        )
        if isinstance(companies, ToolFailure):
            return companies
        try:
            company_list = json.loads(companies).get("searchResults", [])
        except (json.JSONDecodeError, AttributeError):
            return self._failure("ScrapeCreators /search/companies returned non-JSON")

        page_id = self._pick_page(company_list, name)
        if page_id is None:
            # Unresolved official page -> empty, not noisy keyword ads.
            return SourceResult(
                source=self.name,
                url=url,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        # Step 2 — fetch that page's ads.
        ads_resp = await self._request(
            HttpRequest(
                "GET",
                f"{SCRAPECREATORS_BASE}/facebook/adLibrary/company/ads",
                params={"pageId": page_id},
                headers=auth,
            )
        )
        if isinstance(ads_resp, ToolFailure):
            return ads_resp
        try:
            ads = json.loads(ads_resp).get("results", [])
        except (json.JSONDecodeError, AttributeError):
            return self._failure("ScrapeCreators /company/ads returned non-JSON")

        if not ads:
            # The gotcha, handled as data: empty is a legitimate result the orchestrator narrates.
            return SourceResult(
                source=self.name,
                url=url,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        return SourceResult(
            source=self.name,
            url=url,
            fetched_at=datetime.now(UTC),
            raw_excerpt=self._format_ads_excerpt(ads, url, f"page_id {page_id}"),
            status="ok",
        )

    async def _apify_fallback(self, advertiser_domain: str) -> SourceResult | ToolFailure:
        """Apify fallback when ScrapeCreators fails. Self-gates on the Apify key.

        Accepts either ``APIFY_API_KEY`` (the project's canonical name per CLAUDE.md) or
        ``APIFY_API_TOKEN`` (Apify's own conventional name) — so a user's existing .env
        works without renaming.
        """
        key = (os.environ.get("APIFY_API_KEY") or os.environ.get("APIFY_API_TOKEN") or "").strip()
        url = f"meta-ad-library:{advertiser_domain}"
        if not key:
            return self._failure("APIFY_API_KEY not set; Apify fallback unavailable")

        name = self._derive_name(advertiser_domain)
        sync_url = f"{APIFY_BASE}/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items?token={key}"
        resp = await self._request(
            HttpRequest(
                "POST",
                sync_url,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json_body={
                    "startUrls": [{"url": f"https://www.facebook.com/{name}"}],
                    "resultsLimit": APIFY_RESULTS_LIMIT,
                },
                timeout=APIFY_TIMEOUT_S,
            )
        )
        if isinstance(resp, ToolFailure):
            return resp
        try:
            ads = json.loads(resp)
        except (json.JSONDecodeError, TypeError):
            return self._failure("Apify sync run returned non-JSON")
        if not isinstance(ads, list):
            return self._failure("Apify sync run returned a non-array dataset")

        # The actor returns HTTP 201 with {error: "no_items", errorDescription: ...} rows
        # when a startUrl has no ads (or the page URL doesn't resolve to an ad-bearing
        # page) — those are not real ads. Filter them out before the empty check so we
        # never ship a bogus 'ok' with empty fields; a no-ads result is 'empty', honestly.
        ads = [a for a in ads if isinstance(a, dict) and "error" not in a]

        if not ads:
            return SourceResult(
                source=self.name,
                url=url,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )
        return SourceResult(
            source=self.name,
            url=url,
            fetched_at=datetime.now(UTC),
            raw_excerpt=self._format_ads_excerpt(ads, url, f"apify fallback, facebook.com/{name}"),
            status="ok",
        )

    # --- helpers ---

    @staticmethod
    def _derive_name(domain: str) -> str:
        """gusto.com -> gusto; www.deel.com -> deel; https://x.com/path -> x."""
        d = domain.lower().split("://")[-1]
        d = d.removeprefix("www.")
        return d.split("/")[0].split(".")[0] or domain

    @staticmethod
    def _ad_copy(ad: dict) -> str:
        """Pull the ad body text from snapshot.body.text (or the first card body).

        Same path for ScrapeCreators and Apify — both expose snapshot.body.text.
        """
        snap = ad.get("snapshot") or {}
        body = snap.get("body")
        if isinstance(body, dict) and body.get("text"):
            return body["text"]
        if isinstance(body, str) and body:
            return body
        for card in snap.get("cards") or []:
            cb = card.get("body")
            if isinstance(cb, dict) and cb.get("text"):
                return cb["text"]
            if isinstance(cb, str) and cb:
                return cb
        return ""

    @staticmethod
    def _ad_start(ad: dict) -> str:
        """Ad start date — ScrapeCreators (start_date_string/start_date) or Apify
        (startDateFormatted/startDate). Empty string when absent."""
        return (
            ad.get("start_date_string")
            or ad.get("start_date")
            or ad.get("startDateFormatted")
            or str(ad.get("startDate") or "")
            or ""
        )

    @staticmethod
    def _ad_regions(ad: dict) -> list:
        """Targeted/reached countries — ScrapeCreators snake_case or Apify camelCase."""
        return ad.get("targeted_or_reached_countries") or ad.get("targetedOrReachedCountries") or []

    @staticmethod
    def _ad_active(ad: dict) -> object:
        """Active status — ScrapeCreators is_active or Apify isActive. None when absent."""
        if "is_active" in ad:
            return ad.get("is_active")
        return ad.get("isActive")

    def _format_ads_excerpt(self, ads: list[dict], url: str, page_label: str) -> str:
        """Render ads into the D5 excerpt format (start_date, active, regions, copy)."""
        lines = [f"### SOURCE: {url} ({page_label})"]
        for ad in ads:
            copy = self._ad_copy(ad)
            lines.append(
                f"- start_date={self._ad_start(ad)} | active={self._ad_active(ad)} "
                f"| regions={self._ad_regions(ad)} | copy: {copy[:200]}"
            )
        return "\n".join(lines)

    @staticmethod
    def _pick_page(companies: list[dict], target: str) -> str | None:
        """Choose the competitor's official page_id from /search/companies results.

        Exact name match (case-insensitive) preferred. Failing that, a conservative
        soft match (name starts with target AND the page is verified or in a B2B/
        financial category) — never a coincidental match like a coffee brand. Ties
        broken by verified status, then B2B category, then like count.
        """
        tgt = target.lower()
        exact = [c for c in companies if (c.get("name") or "").lower() == tgt]
        if exact:
            pool = exact
        else:
            pool = [
                c
                for c in companies
                if (c.get("name") or "").lower().startswith(tgt)
                and (
                    ((c.get("verification") or "") not in ("", "NOT_VERIFIED"))
                    or any(b in (c.get("category") or "").lower() for b in _BIZ_CATEGORIES)
                )
            ]
        if not pool:
            return None

        def score(c: dict) -> tuple:
            ver = c.get("verification") or ""
            verified = 0 if ver not in ("", "NOT_VERIFIED") else 1  # verified first
            cat = (c.get("category") or "").lower()
            biz = 0 if any(b in cat for b in _BIZ_CATEGORIES) else 1  # B2B category first
            likes = -(c.get("likes") or 0)  # more likes first
            return (verified, biz, likes)

        pool.sort(key=score)
        return pool[0].get("page_id")

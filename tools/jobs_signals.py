"""jobs_signals — Firecrawl scrape of a competitor's public careers page.

Marketing/sales/growth/enterprise hires signal ICP/segment focus (upmarket push vs
SMB-core). The tool accepts a domain or an explicit careers URL. If the default
/careers path returns empty, it falls back to /jobs (max 2 scrape attempts).

Eng review D1: routes through the shared transport (bearer auth). D6: the returned
markdown is consumed by Haiku extraction — role titles + departments are flagged for
marketing/sales/growth/enterprise/SMB signals.
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
MAX_EXCERPT_CHARS = 8000


class JobsSignalsArgs(BaseModel):
    domain: str = Field(description="Competitor domain, e.g. gusto.com")
    careers_url: str | None = Field(
        default=None,
        description="Explicit careers page URL. If omitted, derived from domain.",
    )


class JobsSignalsTool(BaseTool):
    name = "jobs_signals"
    description = (
        "Scrape a competitor's public careers page via Firecrawl. Returns job role "
        "titles and departments (marketing, sales, growth, enterprise, SMB) as "
        "markdown. Use this to infer ICP/segment signals — e.g. a surge in enterprise "
        "AE hires suggests an upmarket push."
    )
    args_schema = JobsSignalsArgs
    required_env = ["FIRECRAWL_API_KEY"]

    async def run(self, domain: str, careers_url: str | None = None) -> SourceResult | ToolFailure:
        auth = bearer("FIRECRAWL_API_KEY")

        # Try at most 2 URLs. Hard errors (500, non-JSON, transport failure) on the
        # primary URL propagate immediately — only 404s / content-emptiness trigger
        # fallback. On the last fallback, any failure propagates as-is.
        primary_url = careers_url or f"https://{domain}/careers"
        fallback_url = None if careers_url else f"https://{domain}/jobs"

        # Primary attempt.
        result = await self._scrape(primary_url, auth)
        if isinstance(result, ToolFailure):
            # Propagate immediate unless it's a clear 404 (page not found) with a
            # fallback available — try the fallback.
            if fallback_url is not None and "HTTP 404" in result.reason:
                result = await self._scrape(fallback_url, auth)
            return result
        if result.status == "ok":
            return result

        # Primary returned empty — try fallback if available.
        if fallback_url is not None:
            result = await self._scrape(fallback_url, auth)
            if isinstance(result, ToolFailure):
                return result
            if result.status == "ok":
                return result

        # Both URLs exhausted — return empty.
        label = careers_url or f"https://{domain}"
        return SourceResult(
            source=self.name,
            url=label,
            fetched_at=datetime.now(UTC),
            raw_excerpt="",
            status="empty",
        )

    async def _scrape(self, url: str, auth: dict[str, str]) -> SourceResult | ToolFailure:
        resp = await self._request(
            HttpRequest(
                "POST",
                f"{FIRECRAWL_BASE}/scrape",
                json_body={"url": url, "formats": ["markdown"]},
                headers=auth,
            )
        )
        if isinstance(resp, ToolFailure):
            return resp
        try:
            md = json.loads(resp).get("data", {}).get("markdown", "")
        except (json.JSONDecodeError, AttributeError):
            return self._failure("Firecrawl /scrape returned non-JSON")

        if not md or len(md.strip()) < 50:
            # Page exists but has no meaningful job content.
            return SourceResult(
                source=self.name,
                url=url,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        if len(md) > MAX_EXCERPT_CHARS:
            md = md[:MAX_EXCERPT_CHARS] + "\n\n[... truncated to bound tokens]"

        return SourceResult(
            source=self.name,
            url=url,
            fetched_at=datetime.now(UTC),
            raw_excerpt=f"### SOURCE: {url}\n{md}",
            status="ok",
        )

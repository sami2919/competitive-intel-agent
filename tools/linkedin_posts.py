"""linkedin_posts — Apify actor for public LinkedIn company posts (no login).

Actor: harvestapi/linkedin-company-posts (Apify Store, "No Cookies", pay-per-event
~$0.001/query), called through the shared transport via run-sync-get-dataset-items —
same pattern as the Apify fallback in meta_ads. Public posts only; no login. Extraction
(themes, cadence, launch signals) happens downstream in the Haiku extractor off the
structured excerpt.

Probe note (2026-07-27): automation-lab/linkedin-company-posts-scraper — the obvious
first pick — runs SUCCEEDED but returns 0 items even for its own example input; the
harvestapi actor returns real posts. Some companies (e.g. gusto) legitimately yield 0
public posts — that is status="empty", narrated, never retried.

Token: accepts APIFY_API_KEY (project-canonical) or APIFY_API_TOKEN (Apify-conventional),
resolved at call time — so required_env stays [] and the tool self-gates with a typed
ToolFailure when unset (mirrors meta_ads._apify_fallback; keeps `make demo` green).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ledger.models import SourceResult, ToolFailure
from tools._base import BaseTool
from tools._transport import HttpRequest

APIFY_BASE = "https://api.apify.com/v2"
ACTOR_PATH = "harvestapi~linkedin-company-posts"
ACTOR_TIMEOUT_S = 120.0  # sync actor runs block until finished; 30s default is too tight
POSTS_LIMIT = 10  # enough for theme/cadence sampling; keeps per-run actor cost trivial
_SNIPPET_LEN = 220


class LinkedInPostsArgs(BaseModel):
    competitor: str = Field(description="Competitor domain, e.g. gusto.com")
    company_url: str | None = Field(
        default=None,
        description=(
            "Explicit LinkedIn company page URL, e.g. https://www.linkedin.com/company/gusto. "
            "Omit to derive the slug from the domain (gusto.com -> /company/gusto)."
        ),
    )


class LinkedInPostsTool(BaseTool):
    name = "linkedin_posts"
    description = (
        "Fetch recent public LinkedIn company posts (text, date, likes, URL) via the "
        "Apify LinkedIn company-posts actor. Returns structured post lines the extractor "
        "uses for social themes, launch signals, and engagement."
    )
    args_schema = LinkedInPostsArgs
    required_env: list[str] = []  # self-gates at runtime (APIFY_API_KEY or APIFY_API_TOKEN)

    async def run(
        self, competitor: str, company_url: str | None = None
    ) -> SourceResult | ToolFailure:
        token = (os.environ.get("APIFY_API_KEY") or os.environ.get("APIFY_API_TOKEN") or "").strip()
        if not token:
            return self._failure(
                "APIFY_API_KEY/APIFY_API_TOKEN not set; linkedin_posts unavailable",
                suggestion="skip this source; note LinkedIn coverage gap in the brief",
            )

        target = company_url or self._derive_company_url(competitor)
        resp = await self._request(
            HttpRequest(
                "POST",
                f"{APIFY_BASE}/acts/{ACTOR_PATH}/run-sync-get-dataset-items",
                params={"token": token},
                json_body={"targetUrls": [target], "maxPosts": POSTS_LIMIT},
                timeout=ACTOR_TIMEOUT_S,
            )
        )
        if isinstance(resp, ToolFailure):
            return resp

        items = self._parse_items(resp)
        if items is None:
            return self._failure(
                "Apify actor returned non-JSON or unexpected shape",
                suggestion="skip this source; note it in the brief",
            )
        if not items:
            # No public posts found — a legitimate result, not a failure.
            return SourceResult(
                source=self.name,
                url=target,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        lines = [f"### SOURCE: linkedin:{target}"]
        for item in items[:POSTS_LIMIT]:
            text = str(item.get("content") or "").replace("\n", " ").strip()
            if not text:
                continue
            posted = item.get("postedAt")
            date = ""
            if isinstance(posted, dict):
                date = str(posted.get("date") or "")[:10]  # 2026-07-27T15:06... -> 2026-07-27
            url = str(item.get("linkedinUrl") or "")
            eng = item.get("engagement")
            likes = eng.get("likes") if isinstance(eng, dict) else None
            parts = [
                f"date={date}",
                f"likes={likes if isinstance(likes, int) else '?'}",
                text[:_SNIPPET_LEN],
            ]
            if url:
                parts.append(url)
            lines.append("- " + " | ".join(parts))

        if len(lines) == 1:  # header only — every item lacked text
            return SourceResult(
                source=self.name,
                url=target,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        return SourceResult(
            source=self.name,
            url=target,
            fetched_at=datetime.now(UTC),
            raw_excerpt="\n".join(lines),
            status="ok",
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_company_url(competitor: str) -> str:
        """gusto.com -> https://www.linkedin.com/company/gusto (heuristic; arg overrides)."""
        host = competitor.strip().removeprefix("https://").removeprefix("http://")
        host = host.split("/")[0].removeprefix("www.")
        slug = host.split(".")[0]
        return f"https://www.linkedin.com/company/{slug}"

    @staticmethod
    def _parse_items(body: str) -> list[dict[str, Any]] | None:
        """Dataset items: top-level array, or dict with an 'items'/'data' list. None = bad."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict)]
        if isinstance(data, dict):
            inner = data.get("items") or data.get("data") or []
            if isinstance(inner, list):
                return [i for i in inner if isinstance(i, dict)]
        return None

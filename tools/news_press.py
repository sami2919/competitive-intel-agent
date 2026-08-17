"""news_press — Exa search for competitor news/press coverage.

Exa returns field-level citations (title, url, publishedDate) that feed the Claim
Evidence schema natively. We use raw httpx through the shared transport (D1: no per-vendor
SDKs). Category 'news' filters to press coverage; highlights provide excerpts for
extraction.

Eng review D1: routes through the shared transport (x-api-key header). D6: publishedDate
and url carry zero-token source attribution for evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ledger.models import SourceResult, ToolFailure
from tools._auth import x_api_key
from tools._base import BaseTool
from tools._transport import HttpRequest

EXA_BASE = "https://api.exa.ai"


class NewsPressArgs(BaseModel):
    competitor: str = Field(description="Competitor name or domain, e.g. 'Gusto' or 'gusto.com'")
    num_results: int = Field(default=10, description="Number of news results to fetch (max 10)")


class NewsPressTool(BaseTool):
    name = "news_press"
    description = (
        "Search Exa for a competitor's recent news and press coverage — funding, "
        "product launches, positioning changes, executive moves, and analyst coverage. "
        "Returns curated press results with publication dates and highlight excerpts "
        "for claim extraction."
    )
    args_schema = NewsPressArgs
    required_env = ["EXA_API_KEY"]

    async def run(self, competitor: str, num_results: int = 10) -> SourceResult | ToolFailure:
        auth = x_api_key("EXA_API_KEY")
        query = f"{competitor} news announcements"
        url = f"exa:{query}"

        body = {
            "query": query,
            "type": "auto",
            "category": "news",
            "numResults": num_results,
            "contents": {"highlights": True},
        }

        resp_body = await self._request(
            HttpRequest(
                "POST",
                f"{EXA_BASE}/search",
                json_body=body,
                headers=auth,
            )
        )
        if isinstance(resp_body, ToolFailure):
            return resp_body

        try:
            data: dict = json.loads(resp_body)
        except (json.JSONDecodeError, AttributeError):
            return self._failure("Exa /search returned non-JSON")

        results: list[dict] | None = data.get("results")
        if not results:
            return SourceResult(
                source=self.name,
                url=url,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        lines: list[str] = [f"### SOURCE: exa:{query}"]
        for r in results:
            title = r.get("title", "")
            result_url = r.get("url", "")
            pub_date = r.get("publishedDate", "")
            highlights = r.get("highlights") or []
            # Join highlights with | and strip whitespace, truncate to avoid
            # blowing past the context window for a single line.
            highlight_text = " | ".join(h.strip().replace("\n", " ") for h in highlights if h)
            lines.append(f"- {pub_date} | {result_url} | {title} | {highlight_text}")

        return SourceResult(
            source=self.name,
            url=url,
            fetched_at=datetime.now(UTC),
            raw_excerpt="\n".join(lines),
            status="ok",
        )

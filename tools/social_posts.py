"""social_posts — Firecrawl blog scrape for posting cadence + content pillars.

Scrapes the competitor's company blog (default /blog) via Firecrawl /scrape, then
extracts recent post titles and any available dates. Haiku extraction runs on the
structured excerpt to derive posting cadence, content pillars, and launch themes.

LinkedIn Ad Library integration is a documented stretch goal — ScrapeCreators does
not expose any LinkedIn endpoint as of Phase 0 probing (all vendor paths returned
HTTP 404), so this tool is blog-only with notes for future LinkedIn addition.

Eng review D1: routes through the shared transport (Firecrawl bearer auth).
D6: the excerpt carries per-article source attribution for claim extraction.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ledger.models import SourceResult, ToolFailure
from tools._auth import bearer
from tools._base import BaseTool
from tools._transport import HttpRequest

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"

# Skip-words that indicate a link is navigation, not an article.
_SKIP_WORDS = (
    "see all",
    "read more",
    "explore",
    "all articles",
    "all answers",
    "get started",
    "sign up",
    "try for",
    "learn more",
    "subscribe",
    "follow us",
    "view all",
    "load more",
)

# Common blog path suffixes to try in order.
_BLOG_PATHS = ["/blog", "/resources", "/news", "/company-news"]


class SocialPostsArgs(BaseModel):
    competitor: str = Field(description="Competitor domain, e.g. gusto.com")
    blog_url: str | None = Field(
        default=None,
        description=(
            "Explicit blog URL, e.g. https://gusto.com/resources/articles. "
            "Omit to auto-detect (tries /blog, then /resources)."
        ),
    )


class SocialPostsTool(BaseTool):
    name = "social_posts"
    description = (
        "Scrape a competitor's company blog for recent post titles and dates. "
        "Returns structured recent-post lines that the extractor uses to derive "
        "posting cadence, content pillars, and launch themes."
    )
    args_schema = SocialPostsArgs
    required_env = ["FIRECRAWL_API_KEY"]

    async def run(self, competitor: str, blog_url: str | None = None) -> SourceResult | ToolFailure:
        auth = bearer("FIRECRAWL_API_KEY")

        if blog_url:
            urls_to_try = [blog_url]
        else:
            base = self._base_url(competitor)
            urls_to_try = [f"{base}{path}" for path in _BLOG_PATHS]

        # At most 2 scrape attempts — try /blog first, then one fallback.
        scraped: str | None = None
        used_url: str = ""
        for url in urls_to_try[:2]:
            resp = await self._request(
                HttpRequest(
                    "POST",
                    f"{FIRECRAWL_BASE}/scrape",
                    json_body={"url": url, "formats": ["markdown"]},
                    headers=auth,
                )
            )
            if isinstance(resp, ToolFailure):
                continue
            try:
                md = json.loads(resp).get("data", {}).get("markdown", "")
            except (json.JSONDecodeError, AttributeError):
                return self._failure(
                    f"Firecrawl /scrape returned non-JSON for {url}",
                    suggestion="skip this source; note it in the brief",
                )
            if md and self._has_articles(md):
                scraped = md
                used_url = url
                break

        if not scraped:
            # No blog content found — a legitimate result, not a failure.
            return SourceResult(
                source=self.name,
                url=blog_url or f"https://{competitor}/blog",
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        articles = self._extract_articles(scraped)

        if not articles:
            return SourceResult(
                source=self.name,
                url=used_url,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        lines = [f"### SOURCE: social:{used_url}"]
        for date_str, title_link in articles:
            lines.append(f"- date={date_str} | {title_link}")

        return SourceResult(
            source=self.name,
            url=used_url,
            fetched_at=datetime.now(UTC),
            raw_excerpt="\n".join(lines),
            status="ok",
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _base_url(competitor: str) -> str:
        """Normalise a competitor string to a base URL with scheme."""
        c = competitor.strip()
        if c.startswith("http://") or c.startswith("https://"):
            return c.rstrip("/")
        return f"https://{c}"

    @staticmethod
    def _has_articles(markdown: str) -> bool:
        """Quick heuristic: does this page look like a blog listing with articles?"""
        # Look for bold link text with article-like length or article-path URLs.
        links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", markdown)
        for text, url in links:
            clean = text.replace("**", "").replace("\n", " ").strip()
            if len(clean) > 15 and not any(s in clean.lower() for s in _SKIP_WORDS):
                return True
            # Also check for article-like URL paths.
            if (
                any(
                    p in url
                    for p in ("/blog/", "/resources/", "/company-news/", "/articles/", "/news/")
                )
                and len(clean) > 8
                and not any(s in clean.lower() for s in _SKIP_WORDS)
            ):  # noqa: E501
                return True
        return False

    @staticmethod
    def _extract_articles(markdown: str) -> list[tuple[str, str]]:
        """Extract (date_or_empty, title_markdown) pairs from blog markdown.

        Returns a list ordered by appearance in the page (newest first assumed).
        Deduplicates by link URL.
        """
        seen_urls: set[str] = set()
        articles: list[tuple[str, str]] = []
        lines = markdown.split("\n")
        i = 0

        # Date patterns: "Month DD, YYYY", "DD Month YYYY", "YYYY-MM-DD", "Month YYYY"
        _DATE_PAT = re.compile(
            r"\b("
            r"(?:January|February|March|April|May|June|July|August|September"
            r"|October|November|December)\s+\d{1,2},?\s+\d{4}"  # "January 15, 2026"
            r"|\d{4}-\d{2}-\d{2}"  # "2026-01-15"
            r"|\d{1,2}/\d{1,2}/\d{4}"  # "01/15/2026"
            r")\b"
        )

        while i < len(lines):
            # --- extract all markdown links from this line and the next 2 lines ---
            buf = "\n".join(lines[i : i + 3])
            links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", buf)

            for text, url in links:
                if url in seen_urls:
                    continue
                clean_text = text.replace("**", "").replace("\n", " ").strip()
                # Filter: must be article-like (long text or article URL path).
                is_article = len(clean_text) > 20 and not any(
                    s in clean_text.lower() for s in _SKIP_WORDS
                )
                is_article_url = (
                    any(
                        p in url
                        for p in ("/blog/", "/resources/", "/company-news/", "/articles/", "/news/")
                    )
                    and len(clean_text) > 10
                    and not any(s in clean_text.lower() for s in _SKIP_WORDS)
                )

                if not (is_article or is_article_url):
                    continue

                seen_urls.add(url)

                # Look for a date in the surrounding lines (5 before, 5 after).
                context = "\n".join(lines[max(0, i - 5) : i + 5])
                date_match = _DATE_PAT.search(context)
                date_str = date_match.group(1) if date_match else ""

                title_link = f"[{clean_text}]({url})"
                articles.append((date_str, title_link))

            i += 1

        return articles

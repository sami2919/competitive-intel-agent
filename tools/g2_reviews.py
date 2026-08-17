"""g2_reviews — public G2 review pages scraped via Firecrawl.

G2 blocks bare scrapers, so we route through Firecrawl (bearer auth). The page
includes rating summaries, a pros/cons frequency table (directly surfaces complaint
themes = positioning gaps for counter-marketing), and individual reviews with star
ratings.

Derives the G2 URL from the competitor domain (gusto.com ->
https://www.g2.com/products/gusto/reviews). Accepts an explicit g2_url override.

GOTCHA: G2 may return a CAPTCHA or empty body. We degrade gracefully — a blocked/
empty scrape returns status='empty' (narrated by the orchestrator), never a
ToolFailure unless it's a hard HTTP error from Firecrawl itself.
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
MAX_EXCERPT_CHARS = 8_000


class G2ReviewsArgs(BaseModel):
    competitor: str = Field(description="Competitor domain, e.g. gusto.com")
    g2_url: str | None = Field(
        default=None,
        description="Explicit G2 reviews URL override. If omitted, derived from competitor domain.",
    )


class G2ReviewsTool(BaseTool):
    name = "g2_reviews"
    description = (
        "Scrape public G2 review pages for a competitor via Firecrawl. Returns rating "
        "summaries, pros/cons frequency counts (complaint themes = positioning gaps), "
        "and individual review excerpts with star ratings. May return empty if G2 "
        "blocks the scrape — that is expected and narrated by the orchestrator."
    )
    args_schema = G2ReviewsArgs
    required_env = ["FIRECRAWL_API_KEY"]

    async def run(self, competitor: str, g2_url: str | None = None) -> SourceResult | ToolFailure:
        url = g2_url or self._derive_g2_url(competitor)
        auth = bearer("FIRECRAWL_API_KEY")

        scrape = await self._request(
            HttpRequest(
                "POST",
                f"{FIRECRAWL_BASE}/scrape",
                json_body={"url": url, "formats": ["markdown"]},
                headers=auth,
            )
        )
        if isinstance(scrape, ToolFailure):
            return scrape

        try:
            body = json.loads(scrape)
        except (json.JSONDecodeError, AttributeError):
            return self._failure("Firecrawl /scrape on G2 returned non-JSON")

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return SourceResult(
                source=self.name,
                url=url,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        md = data.get("markdown", "")
        if not md or not self._is_g2_content(md):
            # G2 may block or return a CAPTCHA page — empty is data, not failure.
            return SourceResult(
                source=self.name,
                url=url,
                fetched_at=datetime.now(UTC),
                raw_excerpt="",
                status="empty",
            )

        excerpt = self._build_excerpt(url, md)
        return SourceResult(
            source=self.name,
            url=url,
            fetched_at=datetime.now(UTC),
            raw_excerpt=excerpt,
            status="ok",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_g2_url(competitor: str) -> str:
        """gusto.com -> https://www.g2.com/products/gusto/reviews

        Strips protocol, 'www.', '.com' (or other TLD), and path fragments.
        """
        domain = competitor.lower().split("://")[-1]
        domain = domain.removeprefix("www.")
        slug = domain.split(".")[0]  # first segment before any .com/.io/etc.
        return f"https://www.g2.com/products/{slug}/reviews"

    @staticmethod
    def _is_g2_content(md: str) -> bool:
        """Check if the scraped page is actual G2 review content, not a CAPTCHA/blocked page.

        A real G2 reviews page has star ratings (X/5), mentions "G2", and includes
        review-related content. A blocked/CAPTCHA page has none of these signals.
        """
        has_rating = "/5" in md and any(c.isdigit() for c in md.split("/5")[0][-3:])
        has_g2 = "G2" in md
        has_review = "review" in md.lower()
        return has_rating and has_g2 and has_review

    @staticmethod
    def _build_excerpt(url: str, md: str) -> str:
        """Build a condensed excerpt from the G2 page markdown.

        Structure:
          1. Rating summary (first ~100 lines: star distribution + pitch)
          2. Pros & Cons frequency table (complaint keyword density)
          3. Individual review entries (up to ~5, with star rating + like/dislike)

        Truncated to MAX_EXCERPT_CHARS.
        """
        lines = md.split("\n")
        parts: list[str] = []
        part: list[str] = []

        # --- Section 1: rating summary + product pitch (lines 0-100 roughly) ---
        for line in lines[:120]:
            stripped = line.strip()
            if stripped:
                part.append(stripped)
            elif part:
                # blank line ends a paragraph block
                parts.append("\n".join(part))
                part = []
        if part:
            parts.append("\n".join(part))

        # --- Section 2: Pros & Cons frequency table ---
        for line in lines:
            if "Pros & Cons" in line and len(line) < 80:
                parts.append("### Pros & Cons (frequency)")
                # grab the next few lines that contain the keyword-count pairs
                idx = lines.index(line) + 1
                for j in range(idx, min(idx + 8, len(lines))):
                    line_text = lines[j].strip()
                    if line_text and (
                        "(" in line_text
                        or line_text.startswith("|")
                        or line_text.startswith("Ease of")
                    ):
                        parts.append(line_text)
                break

        # --- Section 3: individual reviews (up to 5) ---
        review_count = 0
        review_lines: list[str] = []
        in_review = False

        for i, line in enumerate(lines):
            stripped = line.strip()
            # A review starts with a quoted title line like: "Review Title"
            # followed by a rating line like "5/5" or "4.5/5"
            if (
                stripped.startswith('"')
                and stripped.endswith('"')
                and len(stripped) > 10
                and i + 1 < len(lines)
                and "/5" in lines[i + 1]
            ):
                if review_count >= 5 and review_lines:
                    parts.append("\n".join(review_lines))
                    review_lines = []
                    break
                if review_lines:
                    parts.append("\n".join(review_lines))
                review_lines = [stripped]
                in_review = True
                continue

            if in_review:
                review_lines.append(stripped)
                # Stop at blank lines after the review body or at the next clear section
                if stripped == "" and len(review_lines) > 8:
                    in_review = False
                    review_count += 1
                    parts.append("\n".join(review_lines))
                    review_lines = []

        if review_lines:
            parts.append("\n".join(review_lines))

        excerpt = "\n\n".join(parts)
        if len(excerpt) > MAX_EXCERPT_CHARS:
            excerpt = excerpt[:MAX_EXCERPT_CHARS] + "\n\n[...truncated]"

        return f"### SOURCE: g2:{url}\n\n{excerpt}"

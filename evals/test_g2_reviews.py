"""g2_reviews tool tests — Firecrawl-scraped G2 review pages.

Three cases:
  1. Populated — real G2 page data with rating summary, pros/cons, reviews
  2. Empty/blocked — G2 blocks the scrape → status='empty', never ToolFailure
  3. Non-JSON response → ToolFailure
"""

from __future__ import annotations

import asyncio
import json

from ledger.models import ToolFailure
from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.g2_reviews import G2ReviewsTool

FIRECRAWL = "https://api.firecrawl.dev/v1"
SCRAPE_URL = f"{FIRECRAWL}/scrape"
G2_REVIEWS_URL = "https://www.g2.com/products/gusto/reviews"


def _populated_markdown() -> str:
    """Simulated G2 markdown matching the real Gusto page shape."""
    return """![Gusto](https://images.g2crowd.com/uploads/optimized_product_banner/image/10962/banner.png)

[Edit](https://my.g2.com/gusto/product_information)

Gusto

By Gusto

4.6/5 (11,719)

###### 4.6 out of 5 stars

[5 star 82%]  [4 star 13%]  [3 star 1%]  [2 star 0%]  [1 star 1%]

G2 reviews are authentic and verified.

Gusto is a comprehensive human resources (HR) solution designed to assist
businesses in managing their payroll, benefits, hiring, and employee management
needs. This platform caters to a wide range of organizations, from startups to
established enterprises.

#### Pros & Cons

Generated from real user reviews

Ease of Use (5642) Payroll Ease (2984) Easy Setup (2807) Simple (2800)
User Interface (2347) Missing Features (614) Poor Customer Support (601)
Payroll Issues (488) Login Issues (416) Limited Customization (409)

Search reviews

Small-Business (50 or fewer emp.) (8878)
Mid-Market (51-1000 emp.) (1661)
Enterprise (>1000 emp.) (47)

"All-in-One HR Made Easy"
5/5

What do you like best about Gusto?
Gusto keeps all of my information in one place—benefits, PTO, time tracking.
It is intuitive and easy to use with a great mobile app.

What do you dislike about Gusto?
The GPS feature for clocking in can have performance issues occasionally.
Limited customization for larger teams.

What problems is Gusto solving?
Gusto helps my business by integrating scheduling, onboarding, benefits,
and time tracking in one place at a reasonable price.

"Good for small business but limited for growth"
4/5

What do you like best about Gusto?
Simple payroll that takes just a few minutes each cycle. Tax filings
are handled automatically which saves time.

What do you dislike about Gusto?
Missing advanced HR features needed as we grow. International support
is nonexistent and multi-state compliance gets complicated.

What problems is Gusto solving?
Payroll automation for a small team. It works well currently but we are
already looking at alternatives because we need a more comprehensive
platform as we scale beyond 50 employees.

"Payroll works but customer support is lacking"
3/5

What do you like best about Gusto?
Payroll processing is straightforward and the UI is clean.

What do you dislike about Gusto?
Customer support response times are slow. When we had a tax filing issue
it took days to resolve. Integration options are limited compared to
competitors.

"Simple but hitting limits"
4.5/5

What do you like best about Gusto?
Great for basic payroll and benefits. Setup was quick.

What do you dislike about Gusto?
No IT or device management features. We had to add separate tools for
SSO and device management. Would love to see more product depth.

"Works great for our small team"
5/5

What do you like best about Gusto?
Very easy to use. Employees love the mobile app.

What do you dislike about Gusto?
Nothing major, though international contractors are a bit tricky
to handle and the reporting could be more powerful.

---

[View All Alternatives](https://www.g2.com/products/gusto/competitors/alternatives)
"""


def _empty_blocked_markdown() -> str:
    """Simulated blocked/CAPTCHA body — happens when G2 blocks the scrape."""
    return """<html>
<head><title>Verify you are human</title></head>
<body>
<h1>Please verify you are a human</h1>
<p>This page is blocked by security checks.</p>
</body>
</html>"""


class TestG2Reviews:
    """Test the G2 reviews tool."""

    def test_populated_returns_ok_with_key_excerpts(self):
        """Populated G2 data should include rating summary, pros/cons, and review entries."""
        key = HttpRequest(
            "POST", SCRAPE_URL, json_body={"url": G2_REVIEWS_URL, "formats": ["markdown"]}
        ).key()
        fixtures = {
            key: HttpResponse(
                status=200,
                body=json.dumps(
                    {
                        "success": True,
                        "data": {"markdown": _populated_markdown()},
                    }
                ),
            )
        }
        result = asyncio.run(
            G2ReviewsTool(transport=ReplayTransport(fixtures)).run(competitor="gusto.com")
        )
        assert result.status == "ok"
        excerpt = result.raw_excerpt
        # Rating summary
        assert "4.6/5" in excerpt
        assert "11,719" in excerpt
        # Pros & Cons frequency table
        assert "Pros & Cons" in excerpt
        assert "Missing Features" in excerpt
        assert "Poor Customer Support" in excerpt
        # Source attribution header
        assert "### SOURCE: g2:" in excerpt
        assert G2_REVIEWS_URL in excerpt
        # Individual review content
        assert "All-in-One HR Made Easy" in excerpt
        assert "5/5" in excerpt
        assert "what do you dislike" in excerpt.lower()

    def test_empty_blocked_returns_empty_not_failure(self):
        """When G2 blocks the scrape (CAPTCHA/empty), return status='empty', not ToolFailure."""
        key = HttpRequest(
            "POST",
            SCRAPE_URL,
            json_body={"url": "https://www.g2.com/products/deel/reviews", "formats": ["markdown"]},
        ).key()
        fixtures = {
            key: HttpResponse(
                status=200,
                body=json.dumps({"success": True, "data": {"markdown": _empty_blocked_markdown()}}),
            )
        }
        result = asyncio.run(
            G2ReviewsTool(transport=ReplayTransport(fixtures)).run(competitor="deel.com")
        )
        assert result.status == "empty"
        assert result.raw_excerpt == ""

    def test_empty_data_field_returns_empty(self):
        """When Firecrawl returns success but no markdown, return status='empty'."""
        key = HttpRequest(
            "POST",
            SCRAPE_URL,
            json_body={
                "url": "https://www.g2.com/products/gusto/reviews",
                "formats": ["markdown"],
            },
        ).key()
        fixtures = {
            key: HttpResponse(
                status=200,
                body=json.dumps({"success": True, "data": {"markdown": ""}}),
            )
        }
        result = asyncio.run(
            G2ReviewsTool(transport=ReplayTransport(fixtures)).run(competitor="gusto.com")
        )
        assert result.status == "empty"
        assert result.raw_excerpt == ""

    def test_non_json_returns_tool_failure(self):
        """Non-JSON response from Firecrawl should be a ToolFailure."""
        key = HttpRequest(
            "POST",
            SCRAPE_URL,
            json_body={
                "url": "https://www.g2.com/products/gusto/reviews",
                "formats": ["markdown"],
            },
        ).key()
        fixtures = {
            key: HttpResponse(status=200, body="<html>not json</html>"),
        }
        result = asyncio.run(
            G2ReviewsTool(transport=ReplayTransport(fixtures)).run(competitor="gusto.com")
        )
        assert isinstance(result, ToolFailure)

    def test_http_error_returns_tool_failure(self):
        """HTTP >=400 from Firecrawl should be a ToolFailure."""
        key = HttpRequest(
            "POST",
            SCRAPE_URL,
            json_body={
                "url": "https://www.g2.com/products/gusto/reviews",
                "formats": ["markdown"],
            },
        ).key()
        fixtures = {
            key: HttpResponse(status=500, body="Internal Server Error"),
        }
        result = asyncio.run(
            G2ReviewsTool(transport=ReplayTransport(fixtures)).run(competitor="gusto.com")
        )
        assert isinstance(result, ToolFailure)

    def test_explicit_g2_url_override(self):
        """When g2_url is provided, it should be used instead of deriving from competitor."""
        explicit = "https://www.g2.com/products/gusto-hr/reviews"
        key = HttpRequest(
            "POST",
            SCRAPE_URL,
            json_body={"url": explicit, "formats": ["markdown"]},
        ).key()
        fixtures = {
            key: HttpResponse(
                status=200,
                body=json.dumps(
                    {
                        "success": True,
                        "data": {"markdown": _populated_markdown()},
                    }
                ),
            )
        }
        result = asyncio.run(
            G2ReviewsTool(transport=ReplayTransport(fixtures)).run(
                competitor="gusto.com", g2_url=explicit
            )
        )
        assert result.status == "ok"
        assert explicit in result.raw_excerpt

    def test_derive_g2_url(self):
        """Verify domain-to-G2-URL derivation works for various inputs."""
        assert G2ReviewsTool._derive_g2_url("gusto.com") == G2_REVIEWS_URL
        assert (
            G2ReviewsTool._derive_g2_url("www.deel.com")
            == "https://www.g2.com/products/deel/reviews"
        )
        assert (
            G2ReviewsTool._derive_g2_url("https://bamboohr.com")
            == "https://www.g2.com/products/bamboohr/reviews"
        )
        assert (
            G2ReviewsTool._derive_g2_url("rippling.com")
            == "https://www.g2.com/products/rippling/reviews"
        )

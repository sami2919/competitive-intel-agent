"""URL health tests — check_url / check_urls_sync via ReplayTransport.

Four cases (matching the spec exactly):
  a) HEAD 200                        -> LIVE
  b) HEAD 404 + CDX with data row    -> DEAD  (resolvable via Wayback)
  c) HEAD 404 + CDX header-only      -> HALLUCINATED
  d) non-http scheme                 -> LIVE/skipped (internal source identifier)

All requests use method "HEAD" for the URL check and "GET" for Wayback CDX.
Fixtures are keyed by HttpRequest(...).key() following the same pattern as
test_wayback_diff.py and test_meta_ads.py.
"""

from __future__ import annotations

import asyncio
import json

from evals.url_health import WAYBACK_CDX, check_url, check_urls_sync
from tools._transport import HttpRequest, HttpResponse, ReplayTransport

# --- Fixture helpers -----------------------------------------------------------


def _ok(url: str, status: int = 200, body: str = "OK") -> HttpResponse:
    return HttpResponse(status=status, body=body)


def _cdx_headers() -> list[list[str]]:
    """Header row returned by the Wayback CDX API."""
    return [["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]]


def _cdx_data() -> list[list[str]]:
    """Header + one data row — a snapshot exists."""
    return _cdx_headers() + [
        [
            "com,example)/",
            "20260415100000",
            "https://www.example.com/",
            "text/html",
            "200",
            "abc123def",
            "5000",
        ]
    ]


def _cdx_fixture(url: str, body: list[list[str]]) -> dict[str, HttpResponse]:
    return {
        HttpRequest(
            "GET", WAYBACK_CDX, params={"url": url, "output": "json", "limit": "1"}
        ).key(): _ok(url, body=json.dumps(body))
    }


def _head_fixture(url: str, status: int = 200) -> dict[str, HttpResponse]:
    return {
        HttpRequest("HEAD", url).key(): _ok(url, status=status),
    }


# --- Tests ---------------------------------------------------------------------


def test_200_returns_live():
    """(a) HEAD 200 -> LIVE with http_status=200."""
    url = "https://example.com"
    fixtures = _head_fixture(url, status=200)
    health = asyncio.run(check_url(url, transport=ReplayTransport(fixtures)))
    assert health.url == url
    assert health.status == "LIVE"
    assert health.http_status == 200
    assert health.detail == ""


def test_404_with_cdx_snapshot_returns_dead():
    """(b) HEAD 404 + CDX with a data row -> DEAD, resolved via Wayback."""
    url = "https://example.com"
    fixtures: dict[str, HttpResponse] = {}
    fixtures.update(_head_fixture(url, status=404))
    fixtures.update(_cdx_fixture(url, _cdx_data()))
    health = asyncio.run(check_url(url, transport=ReplayTransport(fixtures)))
    assert health.url == url
    assert health.status == "DEAD"
    assert health.http_status == 404
    assert "resolved via Wayback" in health.detail


def test_404_with_cdx_header_only_returns_hallucinated():
    """(c) HEAD 404 + CDX header-only (no data rows) -> HALLUCINATED."""
    url = "https://example.com"
    fixtures: dict[str, HttpResponse] = {}
    fixtures.update(_head_fixture(url, status=404))
    fixtures.update(_cdx_fixture(url, _cdx_headers()))
    health = asyncio.run(check_url(url, transport=ReplayTransport(fixtures)))
    assert health.url == url
    assert health.status == "HALLUCINATED"
    assert health.http_status == 404
    assert "no Wayback snapshot" in health.detail


def test_403_returns_live_not_hallucinated():
    """403 = URL exists but access denied (bot-gated, e.g. G2 / company-news) -> LIVE,
    not HALLUCINATED. Hallucination detection (arXiv 2604.03173) must not false-positive
    on gated pages that block automated HEAD requests with 403."""
    url = "https://g2.com/products/deel/reviews"
    fixtures = _head_fixture(url, status=403)
    health = asyncio.run(check_url(url, transport=ReplayTransport(fixtures)))
    assert health.status == "LIVE"
    assert health.http_status == 403
    assert "not hallucinated" in health.detail


def test_5xx_returns_unreachable():
    """5xx = server error, existence undetermined -> UNREACHABLE (not DEAD/HALLUCINATED)."""
    url = "https://flaky.example.com"
    fixtures = _head_fixture(url, status=503)
    health = asyncio.run(check_url(url, transport=ReplayTransport(fixtures)))
    assert health.status == "UNREACHABLE"
    assert health.http_status == 503


def test_non_http_scheme_returns_live_skipped():
    """(d) non-http scheme -> LIVE with 'non-http scheme, skipped' detail."""
    url = "meta-ad-library:gusto.com"
    # No fixtures needed — the function short-circuits before any transport call.
    health = asyncio.run(check_url(url))
    assert health.url == url
    assert health.status == "LIVE"
    assert health.http_status == 0
    assert health.detail == "non-http scheme, skipped"


def test_check_urls_sync_includes_all_results():
    """check_urls_sync processes every URL and returns results in order."""
    urls = [
        "https://example-200.com",
        "https://example-404-no-cdx.com",
        "non-http:identifier",
    ]
    fixtures: dict[str, HttpResponse] = {
        HttpRequest("HEAD", urls[0]).key(): _ok(urls[0], status=200),
        HttpRequest("HEAD", urls[1]).key(): _ok(urls[1], status=404),
        HttpRequest(
            "GET",
            WAYBACK_CDX,
            params={"url": urls[1], "output": "json", "limit": "1"},
        ).key(): _ok(urls[1], body=json.dumps(_cdx_headers())),
    }
    results = check_urls_sync(urls, transport=ReplayTransport(fixtures))
    assert len(results) == 3
    assert results[0].status == "LIVE"
    assert results[0].http_status == 200
    assert results[1].status == "HALLUCINATED"
    assert results[1].http_status == 404
    assert results[2].status == "LIVE"
    assert results[2].http_status == 0

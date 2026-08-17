"""URL health check — validate every cited URL is resolvable (arXiv 2604.03173).

OpenAI Deep Research (arXiv 2604.03173) reported 3.5% hallucinated URLs and 10.1%
non-resolving URLs in its output. This module checks every cited URL from a claims
ledger against both the live web and Wayback CDX to classify each as LIVE, DEAD
(resolvable via Wayback), HALLUCINATED (no live or archived copy), or UNREACHABLE
(transient network error).

Usage:
    health = await check_url("https://gusto.com/pricing")
    # -> URLHealth(url="...", status="LIVE", http_status=200, detail="")

    results = check_urls_sync(["https://gusto.com", "https://deel.com"])
    # -> list[URLHealth]

Transport seam:
    Accepts the same Transport protocol (LiveTransport / ReplayTransport) the 8
    source tools use (tools/_transport.py). All requests use method "HEAD" for the
    URL check and "GET" for the Wayback CDX query.

Non-http(s) URLs:
    URLs with non-http(s) schemes (e.g. "meta-ad-library:gusto.com") are internal
    source identifiers, not real URLs to HEAD-check. They are returned as LIVE with
    detail "non-http scheme, skipped".
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import urlparse

from tools._transport import (
    HttpRequest,
    LiveTransport,
    Transport,
    TransportError,
)

# Wayback CDX endpoint — free, no API key, no auth.
# Returns JSON list-of-lists: header row + data rows (if snapshots exist).
WAYBACK_CDX = "http://web.archive.org/cdx/search/cdx"


@dataclass(frozen=True)
class URLHealth:
    """Result of checking a single URL against the live web and Wayback CDX.

    Fields:
        url:        The URL that was checked.
        status:     Classification: LIVE | DEAD | HALLUCINATED | UNREACHABLE.
        http_status: The HTTP status code from the HEAD request (0 if unreachable or
                     skipped).
        detail:     Human-readable explanation, especially for non-LIVE outcomes.
    """

    url: str
    status: str  # LIVE | DEAD | HALLUCINATED | UNREACHABLE
    http_status: int
    detail: str


async def check_url(url: str, transport: Transport | None = None) -> URLHealth:
    """HEAD-request the URL and, if not reachable, query Wayback CDX for a snapshot.

    The HEAD request determines reachability (2xx/3xx = LIVE). For non-LIVE responses,
    a GET is issued to the Wayback CDX API. If a CDX snapshot exists the URL is DEAD
    (content still available via archive); otherwise it is HALLUCINATED (no evidence
    the URL ever existed). Timeouts and connection errors are UNREACHABLE.

    Non-http(s) schemes are returned as LIVE/skipped with detail "non-http scheme,
    skipped" — they are internal source identifiers (e.g. "meta-ad-library:gusto.com"),
    not real URLs to HEAD-check.
    """
    # Non-http(s) URLs are internal identifiers, skip resolution.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return URLHealth(
            url=url,
            status="LIVE",
            http_status=0,
            detail="non-http scheme, skipped",
        )

    if transport is None:
        transport = LiveTransport()

    # HEAD request to check if the URL is currently reachable.
    try:
        resp = await transport.request(HttpRequest(method="HEAD", url=url))
    except TransportError as exc:
        return URLHealth(url=url, status="UNREACHABLE", http_status=0, detail=str(exc))

    # 2xx or 3xx — URL is live.
    if 200 <= resp.status < 400:
        return URLHealth(url=url, status="LIVE", http_status=resp.status, detail="")

    # 401/403 — the URL EXISTS but access is denied (bot-gated, login-walled, rate-limited).
    # This is NOT a hallucination: the page is real, just HEAD-forbidden. Treat as LIVE so
    # hallucination detection (arXiv 2604.03173) doesn't false-positive on gated pages like
    # G2 reviews or company-news sections that block automated HEAD requests with 403.
    if resp.status in (401, 403):
        return URLHealth(
            url=url,
            status="LIVE",
            http_status=resp.status,
            detail=f"{resp.status} forbidden — URL exists but access denied (bot-gated); not hallucinated",
        )

    # 5xx — server-side error; existence undetermined (transient). Don't conflate with 404.
    if 500 <= resp.status < 600:
        return URLHealth(
            url=url,
            status="UNREACHABLE",
            http_status=resp.status,
            detail=f"{resp.status} server error — existence undetermined",
        )

    # 404 / 410 / other 4xx — the resource isn't there now. Query Wayback CDX: a snapshot
    # means DEAD (archived copy exists); no snapshot means HALLUCINATED (no evidence it ever existed).
    cdx_params: dict[str, str] = {
        "url": url,
        "output": "json",
        "limit": "1",
    }
    try:
        cdx_resp = await transport.request(
            HttpRequest(method="GET", url=WAYBACK_CDX, params=cdx_params)
        )
    except TransportError:
        return URLHealth(
            url=url,
            status="HALLUCINATED",
            http_status=resp.status,
            detail=f"{resp.status}; Wayback CDX unreachable",
        )

    # Parse CDX response.
    try:
        rows = json.loads(cdx_resp.body)
    except (json.JSONDecodeError, TypeError):
        return URLHealth(
            url=url,
            status="HALLUCINATED",
            http_status=resp.status,
            detail=f"{resp.status}; non-JSON Wayback CDX response",
        )

    # CDX returns a list of lists. First element is the header row; subsequent
    # elements are data rows. A data row means a snapshot exists.
    has_snapshot = isinstance(rows, list) and len(rows) > 1

    if has_snapshot:
        return URLHealth(
            url=url,
            status="DEAD",
            http_status=resp.status,
            detail=f"{resp.status}; resolved via Wayback Machine",
        )

    return URLHealth(
        url=url,
        status="HALLUCINATED",
        http_status=resp.status,
        detail=f"{resp.status}; no Wayback snapshot found",
    )


def check_urls_sync(urls: list[str], transport: Transport | None = None) -> list[URLHealth]:
    """Synchronous convenience wrapper — checks each URL via a single event loop.

    Creates a new LiveTransport inside the event loop when transport is None,
    ensuring proper async client lifecycle.
    """

    async def _run() -> list[URLHealth]:
        t = transport if transport is not None else LiveTransport()
        return [await check_url(url, transport=t) for url in urls]

    return asyncio.run(_run())

"""wayback_diff tool tests — Wayback CDX messaging diff (free, no API key).

Three cases per test exemplar (evals/test_meta_ads.py):
  1. Populated  — live page + CDX with snapshots at ~90 and ~180 days
  2. Empty      — CDX header-only (no snapshot data); gracefully degrades
  3. Failure    — CDX returns non-JSON → ToolFailure

ReplayTransport fixtures keyed by HttpRequest(...).key().
No real env vars set (required_env=[] — no auth headers needed).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from ledger.models import ToolFailure
from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.wayback_diff import WaybackDiffTool

# --- Constants ----------------------------------------------------------------

CDX = "http://web.archive.org/cdx/search/cdx"

# Live page fixtures use the same base URL the tool constructs from each domain.
LIVE = "https://gusto.com/"
LIVE_HTML = (
    "<html><body>"
    "<h1>Run Payroll in Minutes</h1>"
    "<p>The all-in-one payroll platform for modern teams.</p>"
    "</body></html>"
)

ARCHIVE_90D = "https://web.archive.org/web/20260415100000id_/https://gusto.com/"
ARCHIVE_90D_HTML = (
    "<html><h1>Simple Payroll for Small Business</h1>"
    "<p>Starting at $40/mo plus $6 per person.</p></html>"
)

ARCHIVE_180D = "https://web.archive.org/web/20260114120000id_/https://gusto.com/"
ARCHIVE_180D_HTML = (
    "<html><h1>Payroll Made Easy</h1><p>For startups and small businesses.</p></html>"
)

# CDX response: list-of-lists with header row + 2 snapshot rows.
# Columns: [urlkey, timestamp, original, mimetype, statuscode, digest, length]
CDX_POPULATED = json.dumps(
    [
        ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
        [
            "com,gusto)/",
            "20260415100000",
            "https://www.gusto.com/",
            "text/html",
            "200",
            "abc123def",
            "5000",
        ],
        [
            "com,gusto)/",
            "20260114120000",
            "https://www.gusto.com/",
            "text/html",
            "200",
            "ghi456jkl",
            "4800",
        ],
    ]
)

# CDX response with only the header row — zero snapshot data.
CDX_EMPTY = json.dumps(
    [
        ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
    ]
)


# --- Helpers -----------------------------------------------------------------


def _cdx_params(url: str, lookback_days: list[int]) -> dict:
    """Params dict the tool sends to the CDX endpoint.

    from/to are computed via the tool's shared _cdx_window method so the fixture key
    matches the tool's request exactly (same day -> same YYYYMMDD bounds).
    """
    from_ts, to_ts = WaybackDiffTool._cdx_window(datetime.now(UTC), lookback_days)
    return {
        "url": url,
        "output": "json",
        "limit": "200",
        "filter": "statuscode:200",
        "collapse": "digest",
        "from": from_ts,
        "to": to_ts,
    }


def _populated_fixtures() -> dict[str, HttpResponse]:
    f: dict[str, HttpResponse] = {}
    # Live page
    f[HttpRequest("GET", LIVE).key()] = HttpResponse(200, LIVE_HTML)
    # CDX query
    f[HttpRequest("GET", CDX, params=_cdx_params(LIVE, [90, 180])).key()] = HttpResponse(
        200, CDX_POPULATED
    )
    # Archived pages (90d and 180d)
    f[HttpRequest("GET", ARCHIVE_90D).key()] = HttpResponse(200, ARCHIVE_90D_HTML)
    f[HttpRequest("GET", ARCHIVE_180D).key()] = HttpResponse(200, ARCHIVE_180D_HTML)
    return f


# --- Tests -------------------------------------------------------------------


def test_populated_diff():
    """Live page + CDX with two snapshots → excerpt compares all three versions."""
    f = _populated_fixtures()
    result = asyncio.run(
        WaybackDiffTool(transport=ReplayTransport(f)).run(
            domain="gusto.com", paths=["/"], lookback_days=[90, 180]
        )
    )
    assert result.status == "ok"
    body = result.raw_excerpt

    # Excerpt has the wayback: source prefix
    assert "### SOURCE: wayback:https://gusto.com/" in body

    # Today's live text appears
    assert "Run Payroll in Minutes" in body

    # 90-day snapshot is present with correct date formatting
    assert "90d ago (2026-04-15)" in body
    assert "Simple Payroll" in body

    # 180-day snapshot is present with correct date formatting
    assert "180d ago (2026-01-14)" in body
    assert "Payroll Made Easy" in body


def test_empty_cdx_returns_no_snapshot_notice():
    """Header-only CDX → gracefully degrades with 'no snapshot found' messages."""
    f: dict[str, HttpResponse] = {
        HttpRequest("GET", "https://nope.com/").key(): HttpResponse(
            200, "<html><h1>Welcome</h1></html>"
        ),
        HttpRequest(
            "GET", CDX, params=_cdx_params("https://nope.com/", [90, 180])
        ).key(): HttpResponse(200, CDX_EMPTY),
    }
    result = asyncio.run(
        WaybackDiffTool(transport=ReplayTransport(f)).run(
            domain="nope.com", paths=["/"], lookback_days=[90, 180]
        )
    )
    assert result.status == "ok"  # live page still provides data
    body = result.raw_excerpt
    assert "today:" in body
    assert "Welcome" in body
    assert "90d ago: no snapshot found" in body
    assert "180d ago: no snapshot found" in body


def test_non_json_cdx_returns_tool_failure():
    """CDX returns non-JSON → ToolFailure (can't parse the snapshot index)."""
    f: dict[str, HttpResponse] = {
        HttpRequest("GET", "https://fail.com/").key(): HttpResponse(200, "<html>ok</html>"),
        HttpRequest("GET", CDX, params=_cdx_params("https://fail.com/", [90])).key(): HttpResponse(
            200, "<html>not json</html>"
        ),
    }
    result = asyncio.run(
        WaybackDiffTool(transport=ReplayTransport(f)).run(
            domain="fail.com", paths=["/"], lookback_days=[90]
        )
    )
    assert isinstance(result, ToolFailure)
    assert "non-JSON" in result.reason

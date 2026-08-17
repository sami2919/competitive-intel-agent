"""wayback_diff — Wayback Machine CDX-based messaging diff (free, no API key).

The ONLY evidence-based 'what changed recently' source (NEVER-cut differentiator).
Diffs today's live page vs archived snapshots at ~90 and ~180 days ago for
homepage and /pricing messaging changes.

Eng review D1: routes through the shared transport (no per-vendor SDK).
Snapshots may be sparse — degrade gracefully, never crash.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from ledger.models import SourceResult, ToolFailure
from tools._base import BaseTool
from tools._transport import HttpRequest

CDX_BASE = "http://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org/web"


class WaybackDiffArgs(BaseModel):
    domain: str = Field(description="Competitor domain, e.g. gusto.com")
    paths: list[str] = Field(
        default=["/", "/pricing"], description="URL paths to diff (e.g. ['/', '/pricing'])"
    )
    lookback_days: list[int] = Field(
        default=[90, 180], description="Lookback windows in days from today"
    )


class WaybackDiffTool(BaseTool):
    name = "wayback_diff"
    description = (
        "Diff a competitor's homepage and /pricing messaging now vs ~90 and ~180 days ago "
        "via the Wayback Machine CDX API. Free, no API key. Returns extracted text from live "
        "and archived pages for message-change analysis."
    )
    args_schema = WaybackDiffArgs
    required_env: list[str] = []

    async def run(
        self,
        domain: str,
        paths: list[str] | None = None,
        lookback_days: list[int] | None = None,
    ) -> SourceResult | ToolFailure:
        base = domain if domain.startswith("http") else f"https://{domain}"
        resolved_paths = paths or ["/", "/pricing"]
        resolved_lookback = sorted((lookback_days or [90, 180]), reverse=True)
        now = datetime.now(UTC)
        from_ts, to_ts = self._cdx_window(now, resolved_lookback)

        page_results: list[str] = []

        for path in resolved_paths:
            url = base.rstrip("/") + path

            # --- Live page ---
            live_resp = await self._request(HttpRequest("GET", url))
            live_text = (
                self._extract_text(live_resp) if not isinstance(live_resp, ToolFailure) else ""
            )

            # --- CDX snapshot query (list-of-lists) ---
            await asyncio.sleep(0.5)
            cdx_resp = await self._request(
                HttpRequest(
                    "GET",
                    CDX_BASE,
                    params={
                        "url": url,
                        "output": "json",
                        "limit": "200",
                        "filter": "statuscode:200",
                        "collapse": "digest",
                        "from": from_ts,
                        "to": to_ts,
                    },
                )
            )
            if isinstance(cdx_resp, ToolFailure):
                return cdx_resp
            try:
                cdx_data = json.loads(cdx_resp)
            except (json.JSONDecodeError, TypeError):
                return self._failure(
                    "Wayback CDX returned non-JSON",
                    suggestion="skip wayback_diff for this run",
                    retriable=True,
                )

            snapshots = self._parse_cdx_data(cdx_data)

            # --- Build excerpt for this path ---
            path_lines = [f"### SOURCE: wayback:{url}"]
            path_lines.append(f"- today: {self._truncate(live_text)}")

            for days in resolved_lookback:
                target_date = now - timedelta(days=days)
                closest = self._closest_snapshot(snapshots, target_date)
                if closest is not None:
                    date_str = f"{closest[:4]}-{closest[4:6]}-{closest[6:8]}"
                    await asyncio.sleep(0.5)
                    archive_url = f"{WAYBACK_BASE}/{closest}id_/{url}"
                    archive_resp = await self._request(HttpRequest("GET", archive_url))
                    if isinstance(archive_resp, ToolFailure):
                        archive_text = "(error fetching archive)"
                    else:
                        archive_text = self._extract_text(archive_resp)
                    path_lines.append(f"- {days}d ago ({date_str}): {self._truncate(archive_text)}")
                else:
                    path_lines.append(f"- {days}d ago: no snapshot found")

            page_results.append("\n".join(path_lines))

        if not page_results:
            return SourceResult(
                source=self.name,
                url=base,
                fetched_at=now,
                raw_excerpt="",
                status="empty",
            )

        return SourceResult(
            source=self.name,
            url=base,
            fetched_at=now,
            raw_excerpt="\n\n".join(page_results),
            status="ok",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(html: str) -> str:
        """Strip HTML tags and return meaningful text."""
        if not isinstance(html, str) or not html:
            return ""
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _truncate(text: str, max_len: int = 300) -> str:
        """Truncate text at word boundary with ellipsis."""
        if len(text) <= max_len:
            return text
        return text[:max_len].rsplit(" ", 1)[0] + "..."

    @staticmethod
    def _parse_cdx_data(data: list) -> list[tuple[str, str]]:
        """Parse CDX list-of-lists into [(timestamp, original), ...].

        Row 0 is the header, row 1+ are data rows.
        Column indices (confirmed via live probe 2026-07-14):
          [0]=urlkey, [1]=timestamp (YYYYMMDDhhmmss), [2]=original URL,
          [3]=mimetype, [4]=statuscode, [5]=digest, [6]=length
        """
        if not isinstance(data, list) or len(data) < 2:
            return []
        results: list[tuple[str, str]] = []
        for row in data[1:]:
            if isinstance(row, list) and len(row) >= 3:
                results.append((str(row[1]), str(row[2])))
        return results

    @staticmethod
    def _closest_snapshot(snapshots: list[tuple[str, str]], target_date: datetime) -> str | None:
        """Find the snapshot timestamp closest to target_date.

        Compares YYYYMMDD numerically (monotonic ordering preserves
        chronological distance within the same era). Returns the full
        timestamp string (YYYYMMDDhhmmss) or None.
        """
        target_ts = target_date.strftime("%Y%m%d")
        best: str | None = None
        best_diff = float("inf")
        for ts, _ in snapshots:
            ts_date = ts[:8]
            diff = abs(int(ts_date) - int(target_ts))
            if diff < best_diff:
                best_diff = diff
                best = ts
        return best

    @staticmethod
    def _cdx_window(now: datetime, lookback_days: list[int]) -> tuple[str, str]:
        """CDX from/to bounds (YYYYMMDD): from = now - max_lookback - 30d buffer, to = now.

        Bounds the query to the lookback window so CDX returns RECENT snapshots. Without
        bounds, CDX returns the oldest `limit` snapshots — for gusto.com that means its
        2007 prior-owner travel-site era, producing garbage 'messaging' from a defunct
        site (the CLM-021 'travel reviews and lifestyle platform' bug). The 30d buffer
        absorbs sparse snapshot cadences so the closest-snapshot picker still has room.
        """
        max_days = max(lookback_days) if lookback_days else 180
        from_dt = now - timedelta(days=max_days + 30)
        return from_dt.strftime("%Y%m%d"), now.strftime("%Y%m%d")

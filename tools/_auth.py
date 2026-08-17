"""Per-vendor auth headers, read from env at call time (eng review D1: no per-vendor SDKs).

Returns an empty dict when the env var is unset, so ReplayTransport tests and `make demo`
(which never set data-API keys) stay green — auth is irrelevant under replay because
HttpRequest.key() excludes headers. LiveTransport calls carry the real header.

Verified schemes (Phase 0 live-wiring):
  - Firecrawl:      Authorization: Bearer <FIRECRAWL_API_KEY>     (docs.firecrawl.dev)
  - ScrapeCreators: x-api-key: <SCRAPECREATORS_API_KEY>           (docs.scrapecreators.com)
"""

from __future__ import annotations

import os


def bearer(env_var: str) -> dict[str, str]:
    """Authorization: Bearer header from an env var. Empty dict if unset (replay-safe)."""
    key = os.environ.get(env_var, "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def x_api_key(env_var: str) -> dict[str, str]:
    """x-api-key header from an env var. Empty dict if unset (replay-safe)."""
    key = os.environ.get(env_var, "").strip()
    return {"x-api-key": key} if key else {}

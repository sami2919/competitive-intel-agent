"""Small shared tool utilities."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse


def parse_utm(url: str) -> dict[str, str] | None:
    """D6 — extract utm_* params from a URL deterministically (no LLM).

    Returns a dict of utm keys -> values, or None if the URL carries no utm_* params.
    """
    if not url:
        return None
    qs = parse_qs(urlparse(url).query)
    utm = {k: v[0] for k, v in qs.items() if k.lower().startswith("utm_") and v}
    return utm or None

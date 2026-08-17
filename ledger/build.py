"""Claim construction from raw extraction output.

Turns a raw claim dict (from the Haiku extractor) into a Claim with deterministic
confidence. The rubric lives in ledger.confidence; UTM parsing in tools._util (D6).

Phase 6: ad claims (ads_paid_social/ads_search) carrying BOTH first_seen and last_seen
(copied verbatim by the extractor from meta_ads/google_ads excerpt text) route through
the D5 ad-performance rubric (score_ads_performance) instead of the general rubric, and
get a deterministic winning/test signal (ledger.signal.classify_ad_signal). Ad claims
without both dates (meta_ads has no end-date field; google_ads details can fail) fall
back to the general rubric — documented gap, not a crash. `refreshed` (variant/A-B
detection) is always False this pass: no signal source for it exists yet (see
DECISIONS.md next-steps) — a real simplification, not a bug.

Defensive at the LLM-output boundary (CLAUDE.md §14: never trust external data):
Haiku occasionally emits an out-of-set category (e.g. 'content' for a blog post) or
omits a field. claim_from_raw normalizes the category to a valid ClaimCategory and
guards missing fields so one mislabeled extraction can't crash the run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

from ledger.confidence import score_ads_performance, score_general_with_kind
from ledger.models import Claim, ClaimCategory, Evidence, SignalTier
from ledger.signal import classify_ad_signal
from tools._util import parse_utm

# Inference categories — claims reasoned rather than directly stated.
INFERRED_CATEGORIES = frozenset({"icp_targeting"})

# Ad categories eligible for the D5 longevity/refresh/expansion rubric.
AD_CATEGORIES = frozenset({"ads_paid_social", "ads_search"})


def _parse_date(value: object) -> datetime | None:
    """Best-effort ISO-date parse -> always timezone-aware (UTC), or None.

    Date-only strings like ``2026-01-01`` parse as naive; we pin them to UTC so
    longevity math (``last_seen - first_seen``) never mixes aware and naive
    datetimes. None on anything malformed — never raises.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


_VALID_CATEGORIES = frozenset(get_args(ClaimCategory))

# Common Haiku mislabels -> canonical category. Deterministic fixed table.
# Default fallback is "messaging" (most general) so a mislabeled claim is preserved
# rather than dropped — the statement + evidence are usually still valid.
_CATEGORY_ALIASES: dict[str, str] = {
    "content": "social_content",
    "social": "social_content",
    "blog": "social_content",
    "ad": "ads_paid_social",
    "ads": "ads_paid_social",
    "paid": "ads_paid_social",
    "paid_social": "ads_paid_social",
    "search": "ads_search",
    "search_ads": "ads_search",
    "news": "recent_change",
    "change": "recent_change",
    "recent": "recent_change",
    "icp": "icp_targeting",
    "targeting": "icp_targeting",
    "position": "positioning",
    "price": "pricing",
    "message": "messaging",
}


def _normalize_category(raw: str | None) -> str:
    """Clamp a Haiku-emitted category to a valid ClaimCategory. Never raises."""
    if not raw:
        return "messaging"
    key = raw.strip().lower()
    if key in _VALID_CATEGORIES:
        return key
    return _CATEGORY_ALIASES.get(key, "messaging")


def claim_from_raw(
    raw: dict,
    claim_id: str,
    competitor: str,
    tool_name: str,
    extracted_by: str,
    *,
    now: datetime | None = None,
) -> Claim:
    """Build a Claim from a raw extraction dict, computing confidence deterministically.

    Category is normalized to a valid ClaimCategory; a missing statement falls back to
    the excerpt so a partially-formed raw claim never raises. The inferred flag is
    computed from the *normalized* category so aliases like 'icp' still get the 0.3
    inferred tier.

    ``now`` is the crawl timestamp used for ``fetched_at`` and as the default
    ``last_seen`` for ad claims whose start date the extractor copied but whose end
    date it omitted (the extractor is told never to invent dates, so it correctly
    leaves ``last_seen`` empty rather than guess today). An ad returned by an active-ads
    library query is, by construction, still running as of this crawl — so defaulting
    ``last_seen`` to the fetch time is an observation, not a guess, and lets the
    ad-longevity signal fire. Inject a fixed ``now`` in tests for determinism.
    """
    crawl_time = now or datetime.now(UTC)
    url = raw.get("source_url", "")
    first_seen = _parse_date(raw.get("first_seen"))
    last_seen = _parse_date(raw.get("last_seen"))
    if first_seen is not None and last_seen is None:
        # Ad-library queries return currently-listed ads; a known start date with no
        # end date means the creative is still running as of this crawl.
        last_seen = crawl_time
    evidence = Evidence(
        source_url=url,
        excerpt=raw.get("excerpt", ""),
        fetched_at=crawl_time,
        utm_params=parse_utm(url),
        first_seen=first_seen,
        last_seen=last_seen,
    )
    category = _normalize_category(raw.get("category"))
    inferred = category in INFERRED_CATEGORIES
    observed_vs_inferred = "inferred" if inferred else "observed"

    signal: SignalTier | None = None
    signal_trace = ""
    if category in AD_CATEGORIES and first_seen is not None and last_seen is not None:
        longevity_days = (last_seen - first_seen).days
        regions = raw.get("regions") or []
        expanded = isinstance(regions, list) and len(regions) > 1
        conf, trace = score_ads_performance(longevity_days, refreshed=False, expanded=expanded)
        signal, signal_trace = classify_ad_signal(longevity_days, expanded)
    else:
        conf, trace = score_general_with_kind([evidence], tool_name=tool_name, inferred=inferred)

    return Claim(
        id=claim_id,
        competitor=competitor,
        category=category,
        statement=raw.get("statement") or raw.get("excerpt") or "(no statement extracted)",
        evidence=[evidence],
        confidence=conf,
        confidence_trace=trace,
        extracted_by=extracted_by,
        observed_vs_inferred=observed_vs_inferred,
        signal=signal,
        signal_trace=signal_trace,
        source_tool=tool_name,
    )

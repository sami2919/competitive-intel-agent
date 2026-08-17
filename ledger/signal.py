"""Deterministic 'what's winning / what's a test' classification (Phase 6).

Two pure functions, no LLM, each with a trace string — same auditability pattern as
ledger/confidence.py. Public data can't prove ad performance or A/B-test intent, so
these are explicitly framed as durability/corroboration PROXIES, not performance facts:

  classify_ad_signal            — single-source ad longevity (build.py, per-claim)
  classify_corroboration_signal — cross-source agreement (clustering.py, per-canonical)

Priority is cross-source over single-source: a claim that gets clustered into a
CanonicalClaim takes classify_corroboration_signal's verdict, never its own
classify_ad_signal verdict — two-or-more channels agreeing on a message is stronger
evidence than one ad's longevity.
"""

from __future__ import annotations

from datetime import datetime

from ledger.confidence import LONGEVITY_THRESHOLD_DAYS
from ledger.models import CanonicalClaim, ClaimCategory, SignalTier

AD_CATEGORIES: frozenset[str] = frozenset({"ads_paid_social", "ads_search"})


def classify_ad_signal(longevity_days: int | None, expanded: bool) -> tuple[SignalTier | None, str]:
    """Single ad's own longevity -> possible_test (new/narrow) vs likely_winner (durable).

    None when longevity is unknown (no date metadata parsed) — never guess.
    `expanded` (multi-region) is noted in the trace but does not change the tier; it's
    already folded into confidence via score_ads_performance's expansion boost.
    """
    if longevity_days is None:
        return None, "no ad date metadata parsed; signal unknown"

    region_note = "; expanded to multiple regions" if expanded else ""
    if longevity_days >= LONGEVITY_THRESHOLD_DAYS:
        return (
            "likely_winner",
            f"running >= {LONGEVITY_THRESHOLD_DAYS}d ({longevity_days}d) — "
            f"durable creative, proxy for performance{region_note}",
        )
    return (
        "possible_test",
        f"running < {LONGEVITY_THRESHOLD_DAYS}d ({longevity_days}d) — "
        f"new/narrow, may be an active test{region_note}",
    )


def classify_corroboration_signal(
    category: ClaimCategory, independent_source_count: int
) -> tuple[SignalTier | None, str]:
    """Cross-source agreement -> durable_pillar (messaging) or cross_channel_winner (ads).

    None below 2 independent sources — CanonicalClaims are always >= 2 members by
    construction, but a merged group can still collapse to 1 distinct source_url (e.g.
    two ad lines from the same tool call), so this stays a real check, not a formality.
    """
    if independent_source_count < 2:
        return None, "single distinct source even after clustering — no corroboration"

    if category in AD_CATEGORIES:
        return (
            "cross_channel_winner",
            f"same message corroborated across {independent_source_count} independent ad sources",
        )
    return (
        "durable_pillar",
        f"same message corroborated across {independent_source_count} independent "
        f"sources — a persistent, cross-channel pillar",
    )


# --- ad-longevity rendering label (output usefulness pass) ------------------

# Practitioner thresholds for reading competitor ad longevity as a winner-proxy
# (adlibrary.com competitive creative analysis guide: <14d testing, 21-45d
# acceptable, 45+d likely strong performer). These label RENDERING tiers only —
# the confidence rubric's 90d threshold (ledger/confidence.py) is unchanged.
_LONGEVITY_LABEL_TIERS: tuple[tuple[int, str], ...] = (
    (90, "proven (90d+) — durable creative, strongest public winner-proxy"),
    (45, "sustained (45-89d) — likely winner emerging"),
    (14, "maturing (14-44d) — test surviving early kill windows"),
)
_LONGEVITY_LABEL_NEW = "just launched (<14d) — active test, no durability signal yet"


def longevity_label(days: int) -> str:
    """Deterministic plain-English label for an ad's days-running (pure, no LLM)."""
    for threshold, label in _LONGEVITY_LABEL_TIERS:
        if days >= threshold:
            return label
    return _LONGEVITY_LABEL_NEW


# --- winning score (trustworthiness pass) -----------------------------------

# Weights for the deterministic winning score (0-100). Transparent + retunable;
# the trace shows each component so a reviewer can challenge the weights, not the
# verdict. corroboration is the heaviest because cross-source agreement is the
# strongest public-data signal; persistence + recency refine it.
_W_CORROBORATION = 40
_W_PERSISTENCE = 30
_W_RECENCY = 30
_PERSISTENCE_SCALE_DAYS = 365  # 1 year of running -> full persistence component
_RECENCY_WINDOWS = (30, 90)  # within 30d -> full; within 90d -> 2/3; else 1/3
_NEUTRAL_PERSISTENCE = 15  # no date metadata -> neutral (corroboration carries it)
_NEUTRAL_RECENCY = 15


def winning_score(canonical: CanonicalClaim, now: datetime) -> tuple[int, str]:
    """Deterministic 0-100 score for a canonical claim: corroboration + persistence + recency.

    corroboration (0-40): independent source count, saturating at 4 sources.
    persistence  (0-30): how long the message has been running/observed, saturating
        at 1 year. Uses ad longevity (last_seen - first_seen) when dates are present,
        else age since first_seen; neutral 15 when no date metadata at all (non-ad
        canonicals usually lack dates — corroboration carries the durability signal).
    recency      (0-30): how recently the claim was observed active. Full within 30d,
        2/3 within 90d, 1/3 otherwise; neutral 15 when no date metadata.

    Returns (score, trace). Pure function — inject a fixed ``now`` in tests.
    """
    n_independent = canonical.independent_source_count
    corroboration = min(n_independent / 4, 1.0) * _W_CORROBORATION

    longevities: list[int] = []
    ages: list[int] = []
    most_recent: list[datetime] = []
    for e in canonical.evidence:
        if e.first_seen is not None and e.last_seen is not None:
            longevities.append(max(0, (e.last_seen - e.first_seen).days))
        if e.first_seen is not None:
            ages.append(max(0, (now - e.first_seen).days))
            most_recent.append(e.last_seen or e.first_seen)
        elif e.last_seen is not None:
            most_recent.append(e.last_seen)

    if longevities:
        persistence = min(max(longevities) / _PERSISTENCE_SCALE_DAYS, 1.0) * _W_PERSISTENCE
    elif ages:
        persistence = min(max(ages) / _PERSISTENCE_SCALE_DAYS, 1.0) * _W_PERSISTENCE
    else:
        persistence = _NEUTRAL_PERSISTENCE

    if most_recent:
        days_since = max(0, (now - max(most_recent)).days)
        if days_since <= _RECENCY_WINDOWS[0]:
            recency = _W_RECENCY
        elif days_since <= _RECENCY_WINDOWS[1]:
            recency = round(_W_RECENCY * 2 / 3)
        else:
            recency = round(_W_RECENCY / 3)
    else:
        recency = _NEUTRAL_RECENCY

    score = round(corroboration + persistence + recency)
    trace = (
        f"winning={score}/100 (corroboration {round(corroboration)}, "
        f"persistence {round(persistence)}, recency {round(recency)})"
    )
    return score, trace

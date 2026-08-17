"""Deterministic confidence rubric. NEVER model vibes.

General rubric:
  2+ independent sources agree        -> 0.9
  single primary (own site/ads)       -> 0.7
  single secondary (press/review)     -> 0.5
  inferred / indirect                 -> 0.3

D5 ads branch (locked in /plan-ceo-review, re-verified in eng review):
  Ad-performance inferences ("this creative is winning") enter at the inferred tier
  and are raised only by deterministic boosts, with a HARD CAP of 0.7:
    base 0.3 (inferred)
    + 0.2 if ad longevity >= 90 days
    + 0.1 if refreshed into new variants          (stackable)
    + 0.1 if expanded to new regions              (stackable)
    cap 0.7, always — reachable at 0.3+0.2+0.1+0.1
  Performance inferences NEVER take the 0.9 corroboration tier: two of the
  competitor's own channels corroborate the *messaging* claim, not the *performance*
  claim. Public data cannot prove performance; nothing ad-performance-related
  ever exceeds 0.7.

Gate: claims < CONFIDENCE_GATE (0.5) go to an "Unverified signals" appendix,
never the brief body.
"""

from __future__ import annotations

from ledger.models import CONFIDENCE_GATE, Evidence

# --- source classification -------------------------------------------------

# A competitor's own channels = primary. Third-party press/reviews = secondary.
PRIMARY_SOURCES = frozenset(
    {
        "crawl_site",  # their own site
        "meta_ads",  # their own ads
        "google_ads",  # their own ads
        "social_posts",  # their own posts
        "linkedin_posts",  # their own posts
        "wayback_diff",  # their own site history
    }
)
SECONDARY_SOURCES = frozenset({"news_press", "g2_reviews", "jobs_signals"})


def source_kind(tool_name: str) -> str:
    """Classify a source tool as 'primary' or 'secondary'."""
    if tool_name in PRIMARY_SOURCES:
        return "primary"
    if tool_name in SECONDARY_SOURCES:
        return "secondary"
    return "secondary"  # default: treat unknown as secondary (lower confidence)


# --- general rubric --------------------------------------------------------


def score_general(evidence: list[Evidence], inferred: bool = False) -> tuple[float, str]:
    """Score a non-ads claim from its evidence.

    Returns (confidence, trace). 'inferred' marks claims that are reasoned rather
    than directly stated (e.g. an ICP inference from jobs signals).
    """
    distinct_urls = {e.source_url for e in evidence}
    n_independent = len(distinct_urls)

    if inferred:
        return 0.3, "inferred 0.3; no direct source"

    if n_independent >= 2:
        return 0.9, f"two-or-more independent sources agree 0.9 ({n_independent} sources)"

    # single source — classify it
    # (evidence is non-empty by model contract; take the first source's kind)
    # NOTE: caller passes evidence tagged with its tool via the ledger, but for
    # the rubric we only need primary vs secondary. The tool name is passed in.
    raise NotImplementedError(
        "Use score_general_with_kind for single-source claims — the rubric needs "
        "to know whether the single source is primary or secondary."
    )


def score_general_with_kind(
    evidence: list[Evidence], tool_name: str, inferred: bool = False
) -> tuple[float, str]:
    """Score a non-ads claim, using the source tool to classify primary/secondary."""
    if inferred:
        return 0.3, "inferred 0.3; no direct source"

    distinct_urls = {e.source_url for e in evidence}
    n_independent = len(distinct_urls)

    if n_independent >= 2:
        return 0.9, f"two-or-more independent sources agree 0.9 ({n_independent} sources)"

    kind = source_kind(tool_name)
    if kind == "primary":
        return 0.7, "single primary source (own channel) 0.7"
    return 0.5, "single secondary source (press/review) 0.5"


# --- D5 ads branch ---------------------------------------------------------

_ADS_BASE = 0.3
_ADS_LONGEVITY_BOOST = 0.2  # ad longevity >= 90 days
_ADS_REFRESH_BOOST = 0.1  # refreshed into new variants (stackable)
_ADS_EXPANSION_BOOST = 0.1  # expanded to new regions (stackable)
_ADS_HARD_CAP = 0.7  # performance inferences never exceed this
LONGEVITY_THRESHOLD_DAYS = 90  # public: ledger/signal.py shares this threshold


def score_ads_performance(
    longevity_days: int, refreshed: bool, expanded: bool
) -> tuple[float, str]:
    """D5 — score an ad-performance inference with deterministic boosts + hard cap.

    Public data cannot prove ad performance, so performance inferences are capped
    at 0.7 and never reach the 0.9 corroboration tier. Cross-channel same-promise
    evidence corroborates the *messaging* claim (which can reach 0.9 via the
    standard two-source tier), NOT the performance claim.
    """
    parts: list[str] = [f"inferred {_ADS_BASE}"]
    score = _ADS_BASE

    if longevity_days >= LONGEVITY_THRESHOLD_DAYS:
        score += _ADS_LONGEVITY_BOOST
        parts.append(f"longevity>={LONGEVITY_THRESHOLD_DAYS}d +{_ADS_LONGEVITY_BOOST}")

    if refreshed:
        score += _ADS_REFRESH_BOOST
        parts.append(f"refresh +{_ADS_REFRESH_BOOST}")

    if expanded:
        score += _ADS_EXPANSION_BOOST
        parts.append(f"expansion +{_ADS_EXPANSION_BOOST}")

    score = min(score, _ADS_HARD_CAP)
    capped = score >= _ADS_HARD_CAP
    path = "performance-capped path" if capped else "performance path"
    trace = f"{'; '.join(parts)}; single channel, {path}; cap {_ADS_HARD_CAP}"
    return score, trace


# --- estimative language (output usefulness pass) ---------------------------

# ICD-203-style estimative wording mapped 1:1 onto the deterministic rubric tiers,
# so the brief can pair every score with words + the evidence basis (intelligence-
# community practice: probability words must travel with their evidentiary basis).
# Pure rendering — the numbers stay the source of truth.
_ESTIMATIVE_TIERS: tuple[tuple[float, str], ...] = (
    (0.9, "high confidence — corroborated by 2+ independent sources"),
    (0.7, "moderate confidence — single primary source (their own channel)"),
    (0.5, "moderate-low confidence — single secondary source (press/reviews)"),
)
_ESTIMATIVE_FLOOR = "low confidence — inferred or unverified"


def estimative_label(confidence: float) -> str:
    """Deterministic ICD-203-style label for a rubric confidence score (pure, no LLM)."""
    for threshold, label in _ESTIMATIVE_TIERS:
        if confidence >= threshold:
            return label
    return _ESTIMATIVE_FLOOR


# --- gate ------------------------------------------------------------------


def is_unverified(confidence: float) -> bool:
    """True if a claim is below the gate and must go to the appendix, not the body."""
    return confidence < CONFIDENCE_GATE

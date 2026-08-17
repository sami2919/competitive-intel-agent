"""claim_from_raw — defensive at the LLM-output boundary.

Haiku occasionally emits out-of-set categories (e.g. 'content' for a blog post) or
omits a field. claim_from_raw must normalize the category to a valid ClaimCategory
and never raise, so one mislabeled extraction can't crash the whole run. This is the
exact failure that killed the first all-8-tools live Gusto run.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ledger.build import claim_from_raw


def _raw(**kw) -> dict:
    base = {
        "statement": "Gusto leads with simple, transparent flat-rate pricing",
        "category": "messaging",
        "source_url": "https://gusto.com/pricing",
        "excerpt": "Simple pricing. No hidden fees. Plans start at $39/mo + $6/user.",
    }
    base.update(kw)
    return base


def test_invalid_category_normalized_not_raised():
    """The crash case: 'content' (a garbled social_content) must not raise."""
    claim = claim_from_raw(
        _raw(category="content"), "CLM-001", "gusto.com", "social_posts", "haiku-4-5/extractor_v1"
    )
    assert claim.category == "social_content"
    assert claim.statement == "Gusto leads with simple, transparent flat-rate pricing"


def test_unknown_category_defaults_to_messaging():
    claim = claim_from_raw(
        _raw(category="nonsense"), "CLM-002", "gusto.com", "crawl_site", "haiku-4-5/extractor_v1"
    )
    assert claim.category == "messaging"


def test_alias_icp_maps_to_icp_targeting_and_marks_inferred():
    """An 'icp' alias normalizes to icp_targeting (an inferred category) -> confidence 0.3."""
    claim = claim_from_raw(
        _raw(category="icp"), "CLM-003", "gusto.com", "crawl_site", "haiku-4-5/extractor_v1"
    )
    assert claim.category == "icp_targeting"
    assert claim.confidence == 0.3
    assert "inferred" in claim.confidence_trace


def test_valid_category_passes_through_unchanged():
    claim = claim_from_raw(
        _raw(category="pricing"), "CLM-004", "gusto.com", "crawl_site", "haiku-4-5/extractor_v1"
    )
    assert claim.category == "pricing"


def test_missing_statement_falls_back_to_excerpt():
    raw = {
        "category": "messaging",
        "source_url": "https://gusto.com",
        "excerpt": "Payroll for small business.",
    }
    claim = claim_from_raw(raw, "CLM-005", "gusto.com", "crawl_site", "haiku-4-5/extractor_v1")
    assert claim.statement == "Payroll for small business."


def test_missing_category_defaults_to_messaging():
    raw = {
        "statement": "Gusto supports cannabis businesses",
        "source_url": "https://gusto.com",
        "excerpt": "...",
    }
    claim = claim_from_raw(raw, "CLM-006", "gusto.com", "crawl_site", "haiku-4-5/extractor_v1")
    assert claim.category == "messaging"


# ---------------------------------------------------------------------------
# Phase 6: observed_vs_inferred + D5 ad-performance wiring
# ---------------------------------------------------------------------------


def test_non_inferred_category_marked_observed():
    claim = claim_from_raw(
        _raw(category="pricing"), "CLM-007", "gusto.com", "crawl_site", "haiku-4-5/extractor_v2"
    )
    assert claim.observed_vs_inferred == "observed"


def test_icp_category_marked_inferred():
    claim = claim_from_raw(
        _raw(category="icp"), "CLM-008", "gusto.com", "crawl_site", "haiku-4-5/extractor_v2"
    )
    assert claim.observed_vs_inferred == "inferred"


def test_ad_claim_with_both_dates_uses_d5_rubric_not_general():
    """longevity >= 90d -> likely_winner, confidence via score_ads_performance (capped 0.7)."""
    raw = _raw(
        category="ads_paid_social",
        source_url="meta-ad-library:gusto.com",
        first_seen="2026-01-01",
        last_seen="2026-07-01",  # > 90 days
        regions=["US", "CA"],
    )
    claim = claim_from_raw(raw, "CLM-009", "gusto.com", "meta_ads", "haiku-4-5/extractor_v2")
    assert claim.signal == "likely_winner"
    assert claim.confidence == 0.6  # base 0.3 + longevity 0.2 + expansion 0.1 (refreshed=False)
    assert "performance" in claim.confidence_trace
    assert claim.evidence[0].first_seen is not None
    assert claim.evidence[0].last_seen is not None


def test_ad_claim_below_longevity_threshold_is_possible_test():
    raw = _raw(
        category="ads_search",
        source_url="google-ads:gusto.com",
        first_seen="2026-06-01",
        last_seen="2026-06-20",  # < 90 days
    )
    claim = claim_from_raw(raw, "CLM-010", "gusto.com", "google_ads", "haiku-4-5/extractor_v2")
    assert claim.signal == "possible_test"
    assert claim.confidence == 0.3  # base only, no boosts


def test_ad_claim_missing_last_seen_defaults_to_crawl_time():
    """The extractor copies an ad's start_date (first_seen) but, told never to invent
    dates, omits last_seen for active ads. An ad returned by an active-ads library
    query is still running as of the crawl, so build.py defaults last_seen to the
    fetch time — an observation, not a guess — and the longevity signal fires."""
    now = datetime(2026, 7, 15, tzinfo=UTC)
    raw = _raw(
        category="ads_paid_social",
        source_url="meta-ad-library:gusto.com",
        first_seen="2026-01-01",  # ~195 days before `now` -> durable
        # last_seen omitted intentionally
    )
    claim = claim_from_raw(
        raw, "CLM-011", "gusto.com", "meta_ads", "haiku-4-5/extractor_v2", now=now
    )
    assert claim.evidence[0].last_seen == now  # defaulted to crawl time
    assert claim.signal == "likely_winner"  # >= 90d longevity
    assert claim.confidence == 0.5  # ad rubric: 0.3 base + 0.2 longevity, no expansion


def test_ad_claim_missing_both_dates_falls_back_to_general_rubric():
    """No ad date metadata at all -> can't compute longevity, so no signal and the
    general rubric applies (single primary source -> 0.7). This stays a real check,
    not a formality: some ad payloads genuinely carry no start_date."""
    raw = _raw(
        category="ads_paid_social",
        source_url="meta-ad-library:gusto.com",
        # neither first_seen nor last_seen present
    )
    claim = claim_from_raw(raw, "CLM-011b", "gusto.com", "meta_ads", "haiku-4-5/extractor_v2")
    assert claim.signal is None
    assert claim.confidence == 0.7  # single primary source, general rubric


def test_non_ad_claim_never_gets_a_signal():
    claim = claim_from_raw(
        _raw(category="pricing"), "CLM-012", "gusto.com", "crawl_site", "haiku-4-5/extractor_v2"
    )
    assert claim.signal is None
    assert claim.signal_trace == ""

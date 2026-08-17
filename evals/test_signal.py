"""Deterministic signal classifier tests (ledger/signal.py) — no LLM involved.

Covers both threshold branches for each classifier plus the "no data" / "no
corroboration" None cases — the honest-uncertainty paths matter as much as the
positive classifications.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ledger.models import CanonicalClaim, Evidence
from ledger.signal import classify_ad_signal, classify_corroboration_signal, winning_score


def test_ad_signal_none_when_no_date_metadata():
    signal, trace = classify_ad_signal(longevity_days=None, expanded=False)
    assert signal is None
    assert "unknown" in trace


def test_ad_signal_possible_test_below_threshold():
    signal, trace = classify_ad_signal(longevity_days=45, expanded=False)
    assert signal == "possible_test"
    assert "45d" in trace


def test_ad_signal_likely_winner_at_or_above_threshold():
    signal, trace = classify_ad_signal(longevity_days=90, expanded=False)
    assert signal == "likely_winner"
    assert "90d" in trace


def test_ad_signal_notes_region_expansion_without_changing_tier():
    below, below_trace = classify_ad_signal(longevity_days=10, expanded=True)
    above, above_trace = classify_ad_signal(longevity_days=200, expanded=True)
    assert below == "possible_test"
    assert above == "likely_winner"
    assert "expanded to multiple regions" in below_trace
    assert "expanded to multiple regions" in above_trace


def test_corroboration_none_below_two_sources():
    signal, trace = classify_corroboration_signal("messaging", independent_source_count=1)
    assert signal is None
    assert "no corroboration" in trace


def test_corroboration_durable_pillar_for_non_ad_category():
    signal, trace = classify_corroboration_signal("messaging", independent_source_count=3)
    assert signal == "durable_pillar"
    assert "3" in trace


def test_corroboration_cross_channel_winner_for_ad_category():
    signal, trace = classify_corroboration_signal("ads_paid_social", independent_source_count=2)
    assert signal == "cross_channel_winner"
    assert "2" in trace


def test_corroboration_ad_search_also_treated_as_ad_category():
    signal, _trace = classify_corroboration_signal("ads_search", independent_source_count=2)
    assert signal == "cross_channel_winner"


# --- winning_score (trustworthiness pass) -----------------------------------


def _canon(
    n_sources: int, first_seen=None, last_seen=None, category="ads_paid_social"
) -> CanonicalClaim:
    evidence = [
        Evidence(
            source_url=f"https://src{i}.com",
            excerpt="x",
            fetched_at=datetime(2026, 7, 15, tzinfo=UTC),
            first_seen=first_seen,
            last_seen=last_seen,
        )
        for i in range(n_sources)
    ]
    return CanonicalClaim(
        id="CAN-test",
        competitor="gusto.com",
        category=category,
        canonical_statement="test",
        member_claim_ids=[f"CLM-00{i}" for i in range(n_sources)],
        evidence=evidence,
        confidence=0.9,
        confidence_trace="test",
    )


NOW = datetime(2026, 7, 15, tzinfo=UTC)


def test_winning_score_max_for_4_sources_year_old_active_ad():
    # 4 sources (full corroboration 40) + 365d longevity (full persistence 30) + active now (recency 30)
    canon = _canon(4, first_seen=datetime(2025, 7, 15, tzinfo=UTC), last_seen=NOW)
    score, trace = winning_score(canon, NOW)
    assert score == 100
    assert "corroboration 40" in trace and "persistence 30" in trace and "recency 30" in trace


def test_winning_score_corroboration_saturates_at_4_sources():
    # 2 sources -> half corroboration (20); ad running 365d, active -> 20+30+30 = 80
    canon = _canon(2, first_seen=datetime(2025, 7, 15, tzinfo=UTC), last_seen=NOW)
    score, trace = winning_score(canon, NOW)
    assert score == 80
    assert "corroboration 20" in trace


def test_winning_score_new_ad_gets_low_persistence():
    # 4 sources but brand new (2d longevity) + active: 40 + ~0 + 30 = 70
    canon = _canon(4, first_seen=datetime(2026, 7, 13, tzinfo=UTC), last_seen=NOW)
    score, trace = winning_score(canon, NOW)
    assert score == 70
    assert "persistence 0" in trace


def test_winning_score_no_date_metadata_uses_neutral_fallbacks():
    # Non-ad canonical with no dates: corroboration 40 + neutral 15 + neutral 15 = 70
    canon = _canon(4, category="messaging")
    score, trace = winning_score(canon, NOW)
    assert score == 70
    assert "persistence 15" in trace and "recency 15" in trace


def test_winning_score_recency_decay_when_not_recently_observed():
    # 4 sources (40) + 365d longevity (30) + last seen 100d ago -> recency 10 => 80
    canon = _canon(
        4,
        first_seen=datetime(2025, 4, 6, tzinfo=UTC),
        last_seen=datetime(2026, 4, 6, tzinfo=UTC),
    )
    score, trace = winning_score(canon, NOW)
    assert score == 80
    assert "recency 10" in trace


# --- deterministic brief-rendering labels (output usefulness pass) -----------


def test_longevity_label_thresholds() -> None:
    from ledger.signal import longevity_label

    assert "active test" in longevity_label(0)
    assert "active test" in longevity_label(13)
    assert "maturing" in longevity_label(14)
    assert "maturing" in longevity_label(44)
    assert "likely winner" in longevity_label(45)
    assert "likely winner" in longevity_label(89)
    assert "proven" in longevity_label(90)
    assert "proven" in longevity_label(545)


def test_estimative_label_maps_rubric_tiers() -> None:
    from ledger.confidence import estimative_label

    assert estimative_label(0.9).startswith("high confidence")
    assert estimative_label(0.7).startswith("moderate confidence")
    assert estimative_label(0.5).startswith("moderate-low confidence")
    assert estimative_label(0.3).startswith("low confidence")

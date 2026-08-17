"""Phase 0 smoke tests — verify the scaffold imports and the D5 rubric is locked.

These are the first green tests; `make eval` must stay green from Phase 0.
The D5 cases encode the exact semantics locked in /plan-ceo-review and re-verified
in /plan-eng-review: 0.3+0.2+0.1+0.1 = 0.7 (cap reachable), perf-never-0.9, gate 0.5.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ledger.confidence import (
    is_unverified,
    score_ads_performance,
    score_general_with_kind,
    source_kind,
)
from ledger.models import (
    CONFIDENCE_GATE,
    CanonicalClaim,
    Claim,
    Evidence,
)


def _ev(url: str) -> Evidence:
    return Evidence(source_url=url, excerpt="x", fetched_at=datetime.now(UTC))


def test_models_import_and_claim_min_evidence():
    """A claim with no evidence is rejected at the model level."""
    with pytest.raises(ValidationError):
        Claim(
            id="CLM-1",
            competitor="gusto",
            category="ads_paid_social",
            statement="x",
            evidence=[],
            confidence=0.7,
            confidence_trace="t",
            extracted_by="haiku-4-5/extractor_v1",
            observed_vs_inferred="observed",
        )
    c = Claim(
        id="CLM-1",
        competitor="gusto",
        category="ads_paid_social",
        statement="x",
        evidence=[_ev("https://gusto.com")],
        confidence=0.7,
        confidence_trace="t",
        extracted_by="haiku-4-5/extractor_v1",
        observed_vs_inferred="observed",
    )
    assert c.evidence[0].source_url == "https://gusto.com"
    assert c.evidence[0].utm_params is None  # Evidence default None (D6)


def test_d5_ads_cap_is_reachable():
    """0.3 + 0.2 (longevity) + 0.1 (refresh) + 0.1 (expansion) = 0.7; cap binds."""
    conf, trace = score_ads_performance(longevity_days=120, refreshed=True, expanded=True)
    assert conf == 0.7
    assert "performance-capped path" in trace


def test_d5_perf_never_exceeds_cap():
    """Even extreme longevity can't push a performance inference past 0.7."""
    conf, _ = score_ads_performance(longevity_days=365, refreshed=True, expanded=True)
    assert conf <= 0.7


def test_d5_base_alone_is_inferred():
    """No boosts -> base 0.3, below the gate -> unverified appendix."""
    conf, trace = score_ads_performance(longevity_days=10, refreshed=False, expanded=False)
    assert conf == 0.3
    assert "inferred 0.3" in trace
    assert is_unverified(conf)


def test_d5_longevity_only():
    conf, _ = score_ads_performance(longevity_days=95, refreshed=False, expanded=False)
    assert conf == 0.5  # 0.3 + 0.2


def test_gate_threshold():
    assert is_unverified(0.49) is True
    assert is_unverified(0.5) is False
    assert CONFIDENCE_GATE == 0.5


def test_general_two_sources_corroborate():
    """2+ independent source URLs -> 0.9 (the corroboration tier D2 makes fireable)."""
    conf, _ = score_general_with_kind(
        [_ev("https://gusto.com"), _ev("https://www.facebook.com/ads/library/?id=1")],
        tool_name="crawl_site",
    )
    assert conf == 0.9


def test_general_single_primary():
    conf, trace = score_general_with_kind([_ev("https://gusto.com")], tool_name="crawl_site")
    assert conf == 0.7
    assert "primary" in trace


def test_general_single_secondary():
    conf, _ = score_general_with_kind([_ev("https://techcrunch.com/x")], tool_name="news_press")
    assert conf == 0.5


def test_source_kind_classification():
    assert source_kind("crawl_site") == "primary"
    assert source_kind("meta_ads") == "primary"
    assert source_kind("linkedin_posts") == "primary"
    assert source_kind("news_press") == "secondary"
    assert source_kind("g2_reviews") == "secondary"


def test_canonical_claim_independent_source_count():
    """CanonicalClaim counts distinct URLs — same URL twice does not corroborate."""
    cc = CanonicalClaim(
        id="CAN-1",
        competitor="gusto",
        category="messaging",
        canonical_statement="price-first SMB payroll",
        member_claim_ids=["CLM-1", "CLM-2"],
        evidence=[_ev("https://gusto.com"), _ev("https://gusto.com"), _ev("https://fb.com/ads/1")],
        confidence=0.9,
        confidence_trace="two independent sources",
    )
    assert cc.independent_source_count == 2  # gusto.com deduped

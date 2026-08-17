"""Layer-1 deterministic evals (CLAUDE.md §7).

Asserts ledger + brief invariants with no network:
  - JSON schema validity    — every Claim validates via model_validate;
                              category in ClaimCategory; confidence in [0,1].
  - Evidence shape          — every claim has >= 1 Evidence; fetched_at is
                              a datetime instance.
  - Confidence recompute    — for a synthetic Claim, recompute confidence via
                              score_general_with_kind / score_ads_performance
                              from its evidence + tool + inferred flag and
                              assert it equals the stored confidence.
                              Tests: primary (0.7), secondary (0.5),
                              inferred (0.3), 2-source (0.9), ads base (0.3),
                              ads full boosted capped (0.7).
  - Grounding               — check_grounding on a synthetic brief + claims:
                              passes for well-grounded, fails for missing citation.
  - Gate                    — is_unverified flags claims < 0.5; does not flag
                              claims at or above 0.5.

All tests offline — synthetic Claim / Evidence objects in-file.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ledger.build import claim_from_raw
from ledger.confidence import (
    is_unverified,
    score_ads_performance,
    score_general_with_kind,
    source_kind,
)
from ledger.grounding import check_grounding
from ledger.models import CONFIDENCE_GATE, Claim, Evidence

# ---------------------------------------------------------------------------
# Helpers — synthetic evidence and claims
# ---------------------------------------------------------------------------


def _evidence(source_url: str = "https://gusto.com/pricing") -> Evidence:
    """Build a single piece of evidence."""
    return Evidence(
        source_url=source_url,
        excerpt="Simple pricing. No hidden fees. Plans start at $39/mo + $6/user.",
        fetched_at=datetime.now(UTC),
    )


def _claim(*, evidence: list[Evidence], confidence: float, trace: str, **kw: object) -> Claim:
    """Build a Claim with defaults for non-salient fields."""
    defaults: dict[str, object] = dict(
        id="CLM-TST",
        competitor="gusto.com",
        category="messaging",
        statement="Test claim statement",
        evidence=evidence,
        confidence=confidence,
        confidence_trace=trace,
        extracted_by="haiku-4-5/extractor_v1",
        observed_vs_inferred="observed",
    )
    defaults.update(kw)
    return Claim(**defaults)


# ---------------------------------------------------------------------------
# 1. JSON schema validity
# ---------------------------------------------------------------------------


class TestSchema:
    """Claim model validation — schema, categories, confidence bounds."""

    def test_well_formed_claim_validates(self):
        """A fully valid Claim passes model_validate round-trip."""
        c = _claim(evidence=[_evidence()], confidence=0.7, trace="primary 0.7")
        assert Claim.model_validate(c.model_dump()) == c

    def test_invalid_category_raises(self):
        """An unknown category string must raise ValidationError."""
        with pytest.raises(ValidationError):
            _claim(
                evidence=[_evidence()],
                confidence=0.7,
                trace="bad cat",
                category="unknown_category",  # type: ignore[arg-type]
            )

    def test_confidence_negative_raises(self):
        """Confidence < 0 must raise ValidationError."""
        with pytest.raises(ValidationError):
            _claim(evidence=[_evidence()], confidence=-0.1, trace="neg")

    def test_confidence_over_one_raises(self):
        """Confidence > 1 must raise ValidationError."""
        with pytest.raises(ValidationError):
            _claim(evidence=[_evidence()], confidence=1.5, trace="over")

    def test_confidence_lower_boundary_valid(self):
        """Confidence == 0.0 is valid (lower inclusive boundary)."""
        c = _claim(evidence=[_evidence()], confidence=0.0, trace="boundary low")
        assert c.confidence == 0.0

    def test_confidence_upper_boundary_valid(self):
        """Confidence == 1.0 is valid (upper inclusive boundary)."""
        c = _claim(evidence=[_evidence()], confidence=1.0, trace="boundary high")
        assert c.confidence == 1.0

    def test_all_claim_categories_accept_known(self):
        """Every value in the ClaimCategory literal should validate."""
        for cat in (
            "messaging",
            "positioning",
            "pricing",
            "ads_paid_social",
            "ads_search",
            "recent_change",
            "icp_targeting",
            "social_content",
        ):
            c = _claim(
                evidence=[_evidence()],
                confidence=0.7,
                trace=cat,
                category=cat,
            )
            assert c.category == cat


# ---------------------------------------------------------------------------
# 2. Every claim has >= 1 evidence
# ---------------------------------------------------------------------------


class TestEvidenceShape:
    """Evidence list invariants — non-empty, datetime parsed."""

    def test_empty_evidence_raises(self):
        """A Claim with an empty evidence list must raise ValidationError."""
        with pytest.raises(ValidationError):
            _claim(evidence=[], confidence=0.7, trace="no evidence")

    def test_fetched_at_is_datetime(self):
        """evidence.fetched_at must be a datetime instance (parsed, not str)."""
        ev = _evidence()
        assert isinstance(ev.fetched_at, datetime)

    def test_multiple_evidence_valid(self):
        """A claim with 2 evidence items validates."""
        c = _claim(
            evidence=[_evidence("https://gusto.com"), _evidence("https://gusto.com/pricing")],
            confidence=0.9,
            trace="two sources 0.9",
        )
        assert len(c.evidence) == 2


# ---------------------------------------------------------------------------
# 3. Confidence recomputation
# ---------------------------------------------------------------------------


class TestConfidenceRecompute:
    """Recompute confidence from evidence + tool + inferred; must match stored value."""

    def test_primary_source(self):
        """Single primary-source evidence -> confidence 0.7."""
        ev = _evidence()
        score, trace = score_general_with_kind([ev], tool_name="crawl_site")
        claim = _claim(evidence=[ev], confidence=score, trace=trace)
        recomputed, _ = score_general_with_kind(
            list(claim.evidence),
            tool_name="crawl_site",
        )
        assert recomputed == claim.confidence == 0.7
        assert source_kind("crawl_site") == "primary"

    def test_secondary_source(self):
        """Single secondary-source evidence -> confidence 0.5."""
        ev = _evidence()
        score, trace = score_general_with_kind([ev], tool_name="news_press")
        claim = _claim(evidence=[ev], confidence=score, trace=trace)
        recomputed, _ = score_general_with_kind(
            list(claim.evidence),
            tool_name="news_press",
        )
        assert recomputed == claim.confidence == 0.5
        assert source_kind("news_press") == "secondary"

    def test_inferred_category(self):
        """Inferred category (icp_targeting) -> confidence 0.3, regardless of tool."""
        ev = _evidence()
        score, trace = score_general_with_kind([ev], tool_name="crawl_site", inferred=True)
        claim = _claim(
            evidence=[ev],
            confidence=score,
            trace=trace,
            category="icp_targeting",
        )
        recomputed, _ = score_general_with_kind(
            list(claim.evidence),
            tool_name="crawl_site",
            inferred=True,
        )
        assert recomputed == claim.confidence == 0.3
        assert "inferred" in trace

    def test_two_independent_sources(self):
        """Two independent URLs -> confidence 0.9 (corroboration tier)."""
        ev1 = _evidence(source_url="https://gusto.com")
        ev2 = _evidence(source_url="https://gusto.com/pricing")
        score, trace = score_general_with_kind([ev1, ev2], tool_name="crawl_site")
        claim = _claim(evidence=[ev1, ev2], confidence=score, trace=trace)
        recomputed, _ = score_general_with_kind(
            list(claim.evidence),
            tool_name="crawl_site",
        )
        assert recomputed == claim.confidence == 0.9
        assert "two-or-more" in trace

    def test_ads_performance_base(self):
        """Ad-performance inference with no boosts -> base 0.3."""
        score, trace = score_ads_performance(
            longevity_days=30,
            refreshed=False,
            expanded=False,
        )
        assert score == 0.3
        assert "inferred 0.3" in trace

    def test_ads_performance_full_boosted_capped(self):
        """Ad-performance with all three boosts -> capped at 0.7.

        0.3 (base) + 0.2 (90d+ longevity) + 0.1 (refresh) + 0.1 (expansion)
        = 0.7, which hits the hard cap.
        """
        score, trace = score_ads_performance(
            longevity_days=180,
            refreshed=True,
            expanded=True,
        )
        assert score == 0.7
        assert "performance-capped" in trace

    def test_ads_performance_longevity_only(self):
        """Ad-performance with longevity but no other boosts -> 0.5."""
        score, trace = score_ads_performance(
            longevity_days=120,
            refreshed=False,
            expanded=False,
        )
        assert score == 0.5
        assert "longevity>=90d" in trace

    def test_claim_from_raw_recompute(self):
        """A claim built via claim_from_raw recomputes to the same confidence."""
        raw = {
            "statement": "Gusto leads with simple pricing",
            "category": "messaging",
            "source_url": "https://gusto.com",
            "excerpt": "Simple pricing. No hidden fees.",
        }
        claim = claim_from_raw(raw, "CLM-RAW", "gusto.com", "crawl_site", "haiku-4-5/extractor_v1")
        recomputed, _ = score_general_with_kind(
            list(claim.evidence),
            tool_name="crawl_site",
        )
        assert recomputed == claim.confidence


# ---------------------------------------------------------------------------
# 4. Grounding
# ---------------------------------------------------------------------------


class TestGrounding:
    """Grounding validator — brief citations must resolve to known claim IDs."""

    def test_well_grounded_brief_passes(self):
        """Every [CLM-XXX] in the brief resolves to a real claim -> passed."""
        claims = [
            _claim(
                id="CLM-001",
                evidence=[_evidence()],
                confidence=0.7,
                trace="primary 0.7",
            ),
            _claim(
                id="CLM-002",
                evidence=[_evidence()],
                confidence=0.5,
                trace="secondary 0.5",
            ),
        ]
        brief = (
            "Gusto positions as an SMB-first payroll solution [CLM-001]. "
            "Their pricing is transparent and flat-rate [CLM-002]."
        )
        report = check_grounding(brief, claims)
        assert report.passed is True
        assert report.missing_from_ledger == []
        assert report.cited_ids == ["CLM-001", "CLM-002"]

    def test_unresolvable_citation_fails(self):
        """A [CLM-XXX] in the brief with no matching claim -> not passed."""
        claims = [
            _claim(
                id="CLM-001",
                evidence=[_evidence()],
                confidence=0.7,
                trace="primary 0.7",
            ),
        ]
        brief = (
            "Gusto positions as SMB-first [CLM-001]. Alleged detail from [CLM-999] is unsupported."
        )
        report = check_grounding(brief, claims)
        assert report.passed is False
        assert "CLM-999" in report.missing_from_ledger

    def test_no_citations_returns_passed(self):
        """A brief with zero [CLM-XXX] references passes vacuously."""
        claims = [_claim(evidence=[_evidence()], confidence=0.7, trace="primary")]
        brief = "Gusto is a payroll company."
        report = check_grounding(brief, claims)
        assert report.passed is True
        assert report.missing_from_ledger == []

    def test_subgate_claim_in_body_fails(self):
        """A sub-gate claim (confidence < 0.5) in the body must be flagged."""
        claims = [
            _claim(
                id="CLM-001",
                evidence=[_evidence()],
                confidence=0.7,
                trace="primary 0.7",
            ),
            _claim(
                id="CLM-004",
                evidence=[_evidence()],
                confidence=0.3,
                category="icp_targeting",
                trace="inferred 0.3",
            ),
        ]
        brief = (
            "Gusto positions as SMB-first [CLM-001]. "
            "An unverified signal about their strategy [CLM-004]."
        )
        report = check_grounding(brief, claims)
        assert report.passed is False
        assert report.missing_from_ledger == []
        assert len(report.subgate_in_body) == 1
        assert report.subgate_in_body[0].citation == "CLM-004"

    def test_subgate_claim_in_appendix_passes(self):
        """A sub-gate claim inside the 'Unverified signals' appendix is fine."""
        claims = [
            _claim(
                id="CLM-001",
                evidence=[_evidence()],
                confidence=0.7,
                trace="primary 0.7",
            ),
            _claim(
                id="CLM-005",
                evidence=[_evidence()],
                confidence=0.4,
                category="icp_targeting",
                trace="inferred 0.4",
            ),
        ]
        brief = (
            "Gusto positions as SMB-first [CLM-001].\n\n"
            "## Unverified signals\n\n"
            "An unverified signal about their strategy [CLM-005]."
        )
        report = check_grounding(brief, claims)
        assert report.passed is True
        assert report.missing_from_ledger == []
        assert report.subgate_in_body == []


# ---------------------------------------------------------------------------
# 5. Gate
# ---------------------------------------------------------------------------


class TestGate:
    """Claims below CONFIDENCE_GATE are 'unverified' — appendix only."""

    def test_below_gate_flagged(self):
        """confidence < 0.5 -> is_unverified returns True."""
        assert is_unverified(CONFIDENCE_GATE - 0.01) is True
        assert is_unverified(0.0) is True
        assert is_unverified(0.49) is True

    def test_at_gate_not_flagged(self):
        """confidence == 0.5 exactly -> not unverified."""
        assert is_unverified(CONFIDENCE_GATE) is False

    def test_above_gate_not_flagged(self):
        """confidence > 0.5 -> not unverified."""
        assert is_unverified(0.7) is False
        assert is_unverified(0.9) is False
        assert is_unverified(1.0) is False

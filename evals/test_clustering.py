"""Clustering tests — Haiku grouping + deterministic confidence rubric.

Covers four acceptance scenarios:
  (a) Two claims, same assertion, different source_urls -> one CanonicalClaim at 0.9
  (b) Group with unknown claim ID -> dropped (defensive), no raise
  (c) Claims about different things -> no groups -> []
  (d) Non-JSON Haiku response -> [] (no raise)
  (e) Three-member group, category plurality from members
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.cost import Usage
from evals.stub import StubClient
from ledger.clustering import cluster_claims
from ledger.models import Claim, Evidence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(source_url: str = "https://gusto.com", **overrides: Any) -> Evidence:
    base: dict[str, Any] = {
        "source_url": source_url,
        "excerpt": "Test evidence",
        "fetched_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Evidence(**base)


def _claim(
    id: str,
    competitor: str = "gusto.com",
    category: str = "messaging",
    statement: str = "Gusto leads with simple pricing",
    evidence: list[Evidence] | None = None,
    confidence: float = 0.7,
    confidence_trace: str = "single primary source (own channel) 0.7",
    extracted_by: str = "test/v1",
) -> Claim:
    return Claim(
        id=id,
        competitor=competitor,
        category=category,
        statement=statement,
        evidence=evidence or [_evidence()],
        confidence=confidence,
        confidence_trace=confidence_trace,
        extracted_by=extracted_by,
        observed_vs_inferred="observed",
    )


def _make_client(haiku_text: str) -> StubClient:
    return StubClient(sonnet_script=[], haiku_text=haiku_text)


# ---------------------------------------------------------------------------
# (a) Two claims, same assertion, different source_urls
#     -> one CanonicalClaim, confidence 0.9, independent_source_count=2
# ---------------------------------------------------------------------------


def test_same_assertion_two_sources_creates_canonical_claim() -> None:
    claims = [
        _claim("CLM-001", evidence=[_evidence("https://gusto.com")]),
        _claim("CLM-002", evidence=[_evidence("https://gusto.com/pricing")]),
    ]
    haiku_text = (
        '[{"canonical_statement": "Gusto leads with simple, transparent pricing", '
        '"member_claim_ids": ["CLM-001", "CLM-002"]}]'
    )
    client = _make_client(haiku_text)

    result, usage = cluster_claims(claims, client, "system prompt")

    assert len(result) == 1
    cc = result[0]
    assert cc.id == "CAN-001"
    assert cc.competitor == "gusto.com"
    assert cc.category == "messaging"
    assert cc.canonical_statement == "Gusto leads with simple, transparent pricing"
    assert sorted(cc.member_claim_ids) == ["CLM-001", "CLM-002"]
    assert cc.independent_source_count == 2
    assert cc.confidence == 0.9
    assert "two-or-more independent sources" in cc.confidence_trace
    assert len(cc.evidence) == 2
    assert isinstance(usage, Usage)
    # Phase 6: non-ad category + 2 independent sources -> durable_pillar
    assert cc.signal == "durable_pillar"
    assert "2" in cc.signal_trace


# ---------------------------------------------------------------------------
# (b) Group with unknown claim ID -> that group is dropped, no raise
# ---------------------------------------------------------------------------


def test_unknown_claim_id_dropped_no_raise() -> None:
    claims = [
        _claim("CLM-001", evidence=[_evidence("https://gusto.com")]),
    ]
    # Haiku returns a group including CLM-999 which is not in the input claims.
    haiku_text = (
        '[{"canonical_statement": "Unknown grouping", "member_claim_ids": ["CLM-001", "CLM-999"]}]'
    )
    client = _make_client(haiku_text)

    result, usage = cluster_claims(claims, client, "system prompt")

    assert result == []
    assert isinstance(usage, Usage)


# ---------------------------------------------------------------------------
# (c) Claims about different things -> Haiku returns no groups -> []
# ---------------------------------------------------------------------------


def test_no_grouping_returns_empty() -> None:
    claims = [
        _claim(
            "CLM-001",
            statement="Gusto leads with simple pricing",
            evidence=[_evidence("https://gusto.com")],
        ),
        _claim(
            "CLM-002",
            statement="Deel focuses on global payroll",
            evidence=[_evidence("https://deel.com")],
        ),
    ]
    haiku_text = "[]"  # no groups returned
    client = _make_client(haiku_text)

    result, usage = cluster_claims(claims, client, "system prompt")

    assert result == []
    assert isinstance(usage, Usage)


# ---------------------------------------------------------------------------
# (d) Non-JSON Haiku response -> [] (no raise)
# ---------------------------------------------------------------------------


def test_non_json_response_returns_empty() -> None:
    claims = [
        _claim("CLM-001", evidence=[_evidence("https://gusto.com")]),
    ]
    haiku_text = "I cannot help with that clustering task."
    client = _make_client(haiku_text)

    result, usage = cluster_claims(claims, client, "system prompt")

    assert result == []
    assert isinstance(usage, Usage)


# ---------------------------------------------------------------------------
# (e) Three-member group: category selected by plurality
# ---------------------------------------------------------------------------


def test_category_from_plurality() -> None:
    """When members have different categories, the most common wins."""
    claims = [
        _claim("CLM-001", category="messaging", evidence=[_evidence("https://gusto.com")]),
        _claim(
            "CLM-002",
            category="messaging",
            evidence=[_evidence("https://gusto.com/pricing")],
        ),
        _claim(
            "CLM-003",
            category="pricing",
            evidence=[_evidence("https://gusto.com/features")],
        ),
    ]
    haiku_text = (
        '[{"canonical_statement": "Gusto has transparent pricing", '
        '"member_claim_ids": ["CLM-001", "CLM-002", "CLM-003"]}]'
    )
    client = _make_client(haiku_text)

    result, usage = cluster_claims(claims, client, "system prompt")

    assert len(result) == 1
    assert result[0].category == "messaging"  # 2 of 3
    assert result[0].independent_source_count == 3
    assert result[0].confidence == 0.9
    assert isinstance(usage, Usage)


# ---------------------------------------------------------------------------
# (f) Ad-category cluster -> cross_channel_winner, not durable_pillar
# ---------------------------------------------------------------------------


def test_ad_category_cluster_gets_cross_channel_winner_signal() -> None:
    claims = [
        _claim(
            "CLM-001",
            category="ads_paid_social",
            statement="Switch to Gusto and save time",
            evidence=[_evidence("meta-ad-library:gusto.com")],
        ),
        _claim(
            "CLM-002",
            category="ads_search",
            statement="Switch to Gusto and save time",
            evidence=[_evidence("google-ads:gusto.com")],
        ),
    ]
    haiku_text = (
        '[{"canonical_statement": "Switch to Gusto and save time", '
        '"member_claim_ids": ["CLM-001", "CLM-002"]}]'
    )
    client = _make_client(haiku_text)

    result, _usage = cluster_claims(claims, client, "system prompt")

    assert len(result) == 1
    assert result[0].signal == "cross_channel_winner"

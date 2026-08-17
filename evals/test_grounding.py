"""Grounding validator tests — synthetic briefs and synthetic Claims.

Covers four acceptance scenarios:
  (a) All citations valid + above gate               -> passed=True
  (b) Missing [CLM-999] not in ledger                -> passed=False
  (c) Sub-gate claim (conf 0.3) cited in body        -> passed=False
  (d) Sub-gate claim cited ONLY in appendix           -> passed=True
"""

from __future__ import annotations

from datetime import UTC, datetime

from ledger.grounding import check_grounding
from ledger.models import CanonicalClaim, Claim, Evidence


def _evidence(**overrides: object) -> Evidence:
    """Minimal valid Evidence factory."""
    base: dict = {
        "source_url": "https://gusto.com",
        "excerpt": "Test evidence",
        "fetched_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Evidence(**base)


def _claim(id: str, confidence: float, **overrides: object) -> Claim:
    """Minimal valid Claim factory."""
    base: dict = {
        "competitor": "gusto.com",
        "category": "messaging",
        "statement": "Test claim",
        "evidence": [_evidence()],
        "confidence_trace": "test",
        "extracted_by": "test/v1",
        "observed_vs_inferred": "observed",
    }
    base.update(overrides)
    return Claim(id=id, confidence=confidence, **base)


# ---------------------------------------------------------------------------
# (a) Happy path — all citations valid, no sub-gate in body
# ---------------------------------------------------------------------------


def test_all_valid_citations_no_subgate() -> None:
    claims = [
        _claim("CLM-001", 0.9),
        _claim("CLM-002", 0.7),
    ]
    brief = (
        "# Gusto Analysis\n\n"
        "Gusto leads with simple pricing [CLM-001].\n"
        "Their target is SMBs [CLM-002].\n"
    )
    report = check_grounding(brief, claims)

    assert report.passed is True
    assert report.missing_from_ledger == []
    assert report.subgate_in_body == []
    assert sorted(report.cited_ids) == ["CLM-001", "CLM-002"]


# ---------------------------------------------------------------------------
# (b) Missing claim — [CLM-999] not in the ledger
# ---------------------------------------------------------------------------


def test_missing_claim_not_in_ledger() -> None:
    claims = [
        _claim("CLM-001", 0.9),
    ]
    brief = (
        "# Gusto Analysis\n\nGusto leads with simple pricing [CLM-001].\nUnknown claim [CLM-999].\n"
    )
    report = check_grounding(brief, claims)

    assert report.passed is False
    assert report.missing_from_ledger == ["CLM-999"]
    assert report.subgate_in_body == []
    assert sorted(report.cited_ids) == ["CLM-001", "CLM-999"]


# ---------------------------------------------------------------------------
# (c) Sub-gate claim (conf 0.3) cited in the body — should be flagged
# ---------------------------------------------------------------------------


def test_subgate_claim_in_body_is_flagged() -> None:
    claims = [
        _claim("CLM-001", 0.9),
        _claim("CLM-002", 0.3),  # below CONFIDENCE_GATE (0.5)
    ]
    brief = (
        "# Gusto Analysis\n\n"
        "Gusto leads with simple pricing [CLM-001].\n"
        "Their target is SMBs [CLM-002].\n"
    )
    report = check_grounding(brief, claims)

    assert report.passed is False
    assert report.missing_from_ledger == []
    assert len(report.subgate_in_body) == 1
    assert report.subgate_in_body[0].citation == "CLM-002"
    assert "confidence=0.3" in report.subgate_in_body[0].issue


# ---------------------------------------------------------------------------
# (d) Sub-gate claim cited ONLY in the "Unverified signals" appendix — allowed
# ---------------------------------------------------------------------------


def test_subgate_claim_in_appendix_allowed() -> None:
    claims = [
        _claim("CLM-001", 0.9),
        _claim("CLM-002", 0.3),  # below CONFIDENCE_GATE
    ]
    brief = (
        "# Gusto Analysis\n\n"
        "Gusto leads with simple pricing [CLM-001].\n\n"
        "## Unverified signals\n\n"
        "The following signals are worth noting: [CLM-002]\n"
    )
    report = check_grounding(brief, claims)

    assert report.passed is True
    assert report.missing_from_ledger == []
    assert report.subgate_in_body == []


# ---------------------------------------------------------------------------
# (e) Sub-gate claim cited in "What Looks Like a Test" — permitted hypothesis zone
# ---------------------------------------------------------------------------
# possible_test claims are ads < 90 days old -> confidence 0.3 (ad base, no
# longevity boost) -> sub-gate by construction. The Test section is *defined* as
# these claims, so it is a permitted zone (the prompt requires hypothesis framing).


def test_subgate_claim_in_test_section_allowed() -> None:
    claims = [
        _claim("CLM-001", 0.9),
        _claim("CLM-030", 0.3),  # possible_test ad, sub-gate
    ]
    brief = (
        "# Gusto Analysis\n\n"
        "## What's Winning\n\n"
        "Gusto's core pitch is corroborated [CLM-001].\n\n"
        "## What Looks Like a Test\n\n"
        "- Hypothesis: [CLM-030] may be testing a flexible-scheduling angle.\n"
    )
    report = check_grounding(brief, claims)

    assert report.passed is True
    assert report.subgate_in_body == []


def test_subgate_claim_in_whats_winning_still_flagged() -> None:
    """The carve-out is narrow: sub-gate in What's Winning must still fail."""
    claims = [
        _claim("CLM-001", 0.9),
        _claim("CLM-030", 0.3),
    ]
    brief = (
        "# Gusto Analysis\n\n"
        "## What's Winning\n\n"
        "- [CLM-030] is a winner.\n\n"
        "## What Looks Like a Test\n\n"
        "(none)\n"
    )
    report = check_grounding(brief, claims)

    assert report.passed is False
    assert len(report.subgate_in_body) == 1
    assert report.subgate_in_body[0].citation == "CLM-030"


# ---------------------------------------------------------------------------
# Mixed — sub-gate in body AND sub-gate in appendix simultaneously
# ---------------------------------------------------------------------------


def test_mixed_body_and_appendix_subgate() -> None:
    """Only body citations of sub-gate claims are flagged, not appendix ones."""
    claims = [
        _claim("CLM-001", 0.9),
        _claim("CLM-002", 0.3),  # in body — should be flagged
        _claim("CLM-003", 0.3),  # in appendix — allowed
    ]
    brief = (
        "# Gusto Analysis\n\n"
        "Gusto leads with simple pricing [CLM-001].\n"
        "Subgate in body [CLM-002].\n\n"
        "## Unverified signals\n\n"
        "[CLM-003] is allowed here.\n"
    )
    report = check_grounding(brief, claims)

    assert report.passed is False
    assert report.missing_from_ledger == []
    assert len(report.subgate_in_body) == 1
    assert report.subgate_in_body[0].citation == "CLM-002"


# ---------------------------------------------------------------------------
# Phase 6: [CAN-xxx] citations (cross-source-corroborated claims)
# ---------------------------------------------------------------------------


def _canonical(id: str, confidence: float, **overrides: object) -> CanonicalClaim:
    base: dict = {
        "competitor": "gusto.com",
        "category": "messaging",
        "canonical_statement": "Gusto leads with simple pricing",
        "member_claim_ids": ["CLM-001", "CLM-002"],
        "evidence": [_evidence(), _evidence(source_url="https://gusto.com/pricing")],
        "confidence_trace": "test",
    }
    base.update(overrides)
    return CanonicalClaim(id=id, confidence=confidence, **base)


def test_can_citation_valid_passes() -> None:
    claims = [_claim("CLM-001", 0.7), _claim("CLM-002", 0.7)]
    canonical = [_canonical("CAN-001", 0.9)]
    brief = "# Gusto Analysis\n\nGusto leads with simple pricing [CAN-001].\n"
    report = check_grounding(brief, claims, canonical)

    assert report.passed is True
    assert "CAN-001" in report.cited_ids


def test_can_citation_missing_from_canonical_claims_fails() -> None:
    claims = [_claim("CLM-001", 0.7)]
    brief = "# Gusto Analysis\n\nCorroborated claim [CAN-999].\n"
    report = check_grounding(brief, claims, canonical_claims=[])

    assert report.passed is False
    assert report.missing_from_ledger == ["CAN-999"]


def test_can_citation_defaults_to_no_canonical_claims_param() -> None:
    """Callers that don't pass canonical_claims (pre-Phase-6 call sites) still work —
    a [CAN-xxx] citation with no canonical_claims list is correctly reported missing."""
    claims = [_claim("CLM-001", 0.7)]
    brief = "# Gusto Analysis\n\n[CLM-001] and an unbacked [CAN-001].\n"
    report = check_grounding(brief, claims)

    assert report.passed is False
    assert report.missing_from_ledger == ["CAN-001"]


# ---------------------------------------------------------------------------
# Phase 7: chat_mode (follow-up + compare answers) — no sections, sub-gate allowed
# ---------------------------------------------------------------------------


def test_chat_mode_subgate_claim_allowed_anywhere() -> None:
    """Chat answers have no section zones; a sub-gate claim is allowed (the follow-up
    prompt requires in-line 'unverified' labeling). Only missing IDs fail."""
    claims = [_claim("CLM-001", 0.9), _claim("CLM-002", 0.3)]
    chat = (
        "On pricing: Gusto charges a flat rate [CLM-001]. "
        "A possible solopreneur push [CLM-002] (unverified)."
    )
    report = check_grounding(chat, claims, chat_mode=True)

    assert report.passed is True
    assert report.subgate_in_body == []


def test_chat_mode_missing_id_still_fails() -> None:
    claims = [_claim("CLM-001", 0.9)]
    chat = "Gusto charges a flat rate [CLM-001]. Unknown detail [CLM-999]."
    report = check_grounding(chat, claims, chat_mode=True)

    assert report.passed is False
    assert report.missing_from_ledger == ["CLM-999"]


def test_brief_mode_still_flags_subgate_in_body() -> None:
    """chat_mode defaults False — the existing brief-mode behavior is unchanged."""
    claims = [_claim("CLM-001", 0.9), _claim("CLM-002", 0.3)]
    brief = "# Brief\nGusto [CLM-001]. Target [CLM-002]."
    report = check_grounding(brief, claims)

    assert report.passed is False
    assert len(report.subgate_in_body) == 1


# ---------------------------------------------------------------------------
# (e) Symmetric gate check — supra-gate (>= 0.5) claims must NOT hide in the
#     "Unverified signals" appendix (live BambooHR run bug, failure_log F14)
# ---------------------------------------------------------------------------


def test_supragate_claim_in_unverified_appendix_flagged() -> None:
    claims = [_claim("CLM-001", 0.9), _claim("CLM-002", 0.5)]
    brief = (
        "# Brief\n\n"
        "## What's Winning\n\nStrong pillar [CLM-001].\n\n"
        "## Unverified signals\n\n"
        "- Launches partner program [CLM-002] (conf 0.5)\n"
    )
    report = check_grounding(brief, claims)

    assert report.passed is False
    assert [f.citation for f in report.supragate_in_appendix] == ["CLM-002"]


def test_supragate_in_body_and_subgate_in_appendix_still_pass() -> None:
    claims = [_claim("CLM-001", 0.7), _claim("CLM-002", 0.3)]
    brief = (
        "# Brief\n\n"
        "## What's Winning\n\nPillar [CLM-001].\n\n"
        "## Unverified signals\n\n- Raw signal [CLM-002] (conf 0.3)\n"
    )
    report = check_grounding(brief, claims)

    assert report.passed is True
    assert report.supragate_in_appendix == []


def test_supragate_in_test_section_not_flagged() -> None:
    # The symmetric check targets the Unverified appendix ONLY — a corroborated ad
    # angle discussed inside "What Looks Like a Test" is odd but not a gate violation.
    claims = [_claim("CLM-001", 0.7)]
    brief = "## What Looks Like a Test\n\n- Hypothesis: may be testing X [CLM-001]\n"
    report = check_grounding(brief, claims)

    assert report.passed is True
    assert report.supragate_in_appendix == []


def test_supragate_check_skipped_in_chat_mode() -> None:
    claims = [_claim("CLM-001", 0.7)]
    brief = "Their unverified signals include X [CLM-001]."
    report = check_grounding(brief, claims, chat_mode=True)

    assert report.passed is True


def test_supragate_canonical_in_appendix_flagged() -> None:
    claims = [_claim("CLM-001", 0.3)]
    canonical = [
        CanonicalClaim(
            id="CAN-001",
            competitor="gusto.com",
            category="messaging",
            canonical_statement="Corroborated pillar",
            member_claim_ids=["CLM-001"],
            evidence=[_evidence(), _evidence(source_url="https://press.example.com")],
            confidence=0.9,
            confidence_trace="2 sources",
        )
    ]
    brief = "## Unverified signals\n\n- Pillar [CAN-001]\n"
    report = check_grounding(brief, claims, canonical)

    assert report.passed is False
    assert [f.citation for f in report.supragate_in_appendix] == ["CAN-001"]

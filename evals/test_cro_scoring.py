"""Deterministic variant rubric — gates reject categorically, weights score by rule."""

from __future__ import annotations

import pytest

from cro.models import Hypothesis, Variant
from cro.scoring import (
    RIPPLING_VOCABULARY,
    count_concrete_referents,
    flesch_kincaid_grade,
    score_variant,
)

HYP = Hypothesis(
    id="HYP-001",
    statement="Position Rippling as the platform you will not outgrow",
    counters_canonical_id="CAN-009",
    segment="30-200 employee migration",
    source_winning_score=80,
    rationale="Gusto's simplicity story breaks at multi-state scale",
)

CANONICAL = "Gusto's simplicity and ease-of-use messaging is a cross-channel winner"


def _variant(**overrides) -> Variant:
    base = dict(
        id="VAR-001",
        hypothesis_id="HYP-001",
        headline="Simplicity that survives 50 states",
        subhead="Run payroll across 12 entities without adding headcount.",
        cta="See how",
        segment="30-200 employee migration",
        changed_elements=["hero"],
        claim_refs=["CAN-009"],
        generated_by="sonnet-5/variant_gen_v1",
    )
    base.update(overrides)
    return Variant(**base)


# --- gates: categorical rejection, score is not computed ---------------------


def test_unsourced_claim_is_rejected_outright():
    scored = score_variant(_variant(), HYP, CANONICAL, unsourced_claims=["cheaper than Gusto"])
    assert scored.shippable is False
    assert scored.reject_reason == "unsourced_claim"
    assert scored.score == 0
    assert "cheaper than Gusto" in scored.trace


def test_multivariate_variant_is_rejected_outright():
    scored = score_variant(_variant(changed_elements=["hero", "cta", "offer"]), HYP, CANONICAL)
    assert scored.shippable is False
    assert scored.reject_reason == "multivariate"
    assert "single variable" in scored.trace


def test_repeated_same_element_is_not_multivariate():
    # ["hero", "hero"] is one independent variable, not two.
    scored = score_variant(_variant(changed_elements=["hero", "hero"]), HYP, CANONICAL)
    assert scored.reject_reason != "multivariate"


# --- specificity: positive test, not a blocklist -----------------------------


def test_vague_copy_with_no_banned_words_still_fails_specificity():
    # The exact case a blocklist misses: no banned phrase, asserts nothing.
    scored = score_variant(
        _variant(headline="Better tools for growing teams", subhead="Get more done today."),
        HYP,
        CANONICAL,
    )
    assert "specificity 0/25" in scored.trace
    assert "asserts nothing checkable" in scored.trace


def test_concrete_copy_earns_full_specificity():
    scored = score_variant(_variant(), HYP, CANONICAL)
    assert "specificity 25/25" in scored.trace


def test_specificity_is_not_dodged_by_synonym_substitution():
    # The point a blocklist cannot make: swapping a "banned" word for a synonym
    # leaves SPECIFICITY untouched, because the check measures concrete referents
    # rather than forbidden strings. (Totals may still differ — "Frictionless" is
    # three syllables to "Seamless"'s two, which legitimately moves readability.)
    vague_a = _variant(headline="Seamless payroll for teams", subhead="Simple and easy.")
    vague_b = _variant(headline="Frictionless payroll for teams", subhead="Simple and easy.")
    a = score_variant(vague_a, HYP, CANONICAL)
    b = score_variant(vague_b, HYP, CANONICAL)
    assert "specificity 0/25" in a.trace
    assert "specificity 0/25" in b.trace


def test_llm_tic_is_noted_but_unscored():
    plain = _variant(headline="Payroll that survives 50 states")
    tic = _variant(headline="Seamless payroll that survives 50 states")
    assert score_variant(tic, HYP, CANONICAL).score == score_variant(plain, HYP, CANONICAL).score
    assert "llm-tic: seamless" in score_variant(tic, HYP, CANONICAL).trace


def test_count_concrete_referents_reports_matches():
    n, hits = count_concrete_referents("Run payroll across 50 states", RIPPLING_VOCABULARY)
    assert n == 3
    assert set(hits) == {"payroll", "50", "states"}


# --- weighted checks ---------------------------------------------------------


def test_message_match_rewards_shared_content_terms():
    on_message = score_variant(
        _variant(headline="Simplicity that survives 50 states"), HYP, CANONICAL
    )
    off_message = score_variant(
        _variant(headline="Device management for 50 states"), HYP, CANONICAL
    )
    assert on_message.score > off_message.score


def test_overlong_headline_loses_length_points():
    long_headline = "Simplicity that survives fifty separate states and every single entity too"
    assert len(long_headline) > 60
    scored = score_variant(_variant(headline=long_headline), HYP, CANONICAL)
    assert "over limit: headline" in scored.trace


def test_segment_mismatch_scores_lower_but_does_not_reject():
    # Deliberately weighted, not a gate — fuzzy boundaries should not hard-fail.
    mismatch = score_variant(_variant(segment="solopreneurs"), HYP, CANONICAL)
    match = score_variant(_variant(), HYP, CANONICAL)
    assert mismatch.score < match.score
    assert mismatch.reject_reason != "segment_mismatch"


def test_flesch_kincaid_ranks_complexity():
    simple = flesch_kincaid_grade("Run payroll fast. It works well.")
    complex_ = flesch_kincaid_grade(
        "Comprehensive multinational compensation administration necessitates sophisticated "
        "infrastructural consolidation."
    )
    assert complex_ > simple


# --- gate --------------------------------------------------------------------


def test_weak_variant_falls_below_gate():
    scored = score_variant(
        _variant(headline="Do more", subhead="", cta="Click here now please", segment=""),
        HYP,
        CANONICAL,
    )
    assert scored.shippable is False
    assert scored.reject_reason == "below_gate"
    assert "below gate 60" in scored.trace


def test_strong_variant_ships_with_full_trace():
    scored = score_variant(_variant(), HYP, CANONICAL)
    assert scored.shippable is True
    assert scored.score >= 60
    for check in ("message_match", "specificity", "length", "readability", "segment_fit"):
        assert check in scored.trace


@pytest.mark.parametrize("score_field", ["message_match", "specificity"])
def test_trace_is_auditable_per_check(score_field):
    assert score_field in score_variant(_variant(), HYP, CANONICAL).trace

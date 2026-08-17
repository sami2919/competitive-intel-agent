"""Ledger -> hypotheses. Deterministic selection, ranking, and segment inference.

Includes a test against the REAL shipped Gusto canonical ledger — if the hypothesis
layer stops reading actual output, that is a regression worth failing on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cro.hypotheses import MIN_CONFIDENCE, build_hypotheses, infer_segment, load_canonical_claims

GUSTO_CANONICAL = Path(__file__).resolve().parents[1] / "outputs" / "gusto.com_canonical.json"


def _claim(cid: str, *, signal=None, win=50, conf=0.9, statement="Gusto does a thing") -> dict:
    return {
        "id": cid,
        "canonical_statement": statement,
        "signal": signal,
        "winning_score": win,
        "confidence": conf,
    }


# --- ranking -----------------------------------------------------------------


def test_cross_channel_winner_outranks_higher_scoring_durable_pillar():
    # Signal dominates score: a proven PAID angle is worth more to counter than a
    # table-stakes pillar, even one scoring higher.
    claims = [
        _claim("CAN-A", signal="durable_pillar", win=100),
        _claim("CAN-B", signal="cross_channel_winner", win=68),
    ]
    assert [h.counters_canonical_id for h in build_hypotheses(claims)] == ["CAN-B", "CAN-A"]


def test_winning_score_breaks_ties_within_a_signal():
    claims = [
        _claim("CAN-LOW", signal="cross_channel_winner", win=60),
        _claim("CAN-HIGH", signal="cross_channel_winner", win=90),
    ]
    assert build_hypotheses(claims)[0].counters_canonical_id == "CAN-HIGH"


def test_unsignalled_claims_rank_last():
    claims = [
        _claim("CAN-NONE", signal=None, win=100),
        _claim("CAN-SIG", signal="durable_pillar", win=10),
    ]
    assert build_hypotheses(claims)[0].counters_canonical_id == "CAN-SIG"


# --- eligibility -------------------------------------------------------------


def test_low_confidence_claims_are_not_eligible():
    claims = [_claim("CAN-WEAK", conf=0.5), _claim("CAN-OK", conf=0.9)]
    ids = [h.counters_canonical_id for h in build_hypotheses(claims)]
    assert ids == ["CAN-OK"]


def test_confidence_threshold_is_inclusive_at_the_primary_tier():
    assert build_hypotheses([_claim("CAN-EDGE", conf=MIN_CONFIDENCE)])


def test_limit_caps_selection():
    claims = [_claim(f"CAN-{i}", win=i) for i in range(10)]
    assert len(build_hypotheses(claims, limit=3)) == 3


def test_hypothesis_ids_are_sequential():
    claims = [_claim(f"CAN-{i}") for i in range(3)]
    assert [h.id for h in build_hypotheses(claims)] == ["HYP-001", "HYP-002", "HYP-003"]


# --- segment inference -------------------------------------------------------


@pytest.mark.parametrize(
    "statement,expected",
    [
        ("Gusto targets solopreneurs and S-corp owners", "solopreneurs"),
        ("Gusto positions switching from competitor HR software", "30-200 employee migration"),
        ("Gusto emphasizes simplicity and ease of use", "30-200 employee migration"),
        # Verb form, not the noun — this is the shipped ledger's top claim (CAN-002)
        # and matching only "simplicity" defaulted it to SMB core.
        ("Gusto positions itself as simplifying payroll and HR", "30-200 employee migration"),
        ("Gusto supports international contractor payments", "global / multi-entity"),
        ("Gusto offers unlimited payroll runs", "SMB core"),
    ],
)
def test_segment_inference(statement, expected):
    segment, _trace = infer_segment(statement)
    assert segment == expected


def test_segment_trace_names_the_deciding_term():
    _segment, trace = infer_segment("Gusto targets solopreneurs")
    assert "solopreneur" in trace


def test_default_segment_is_traced_as_a_default():
    _segment, trace = infer_segment("Gusto exists")
    assert "defaulted" in trace


# --- against the real shipped ledger -----------------------------------------


def test_reads_the_real_gusto_canonical_ledger():
    claims = load_canonical_claims(GUSTO_CANONICAL)
    hypotheses = build_hypotheses(claims, limit=5)

    assert len(hypotheses) == 5
    # CAN-002 is the top cross_channel_winner at 100/100 in the shipped ledger.
    assert hypotheses[0].counters_canonical_id == "CAN-002"
    # Every hypothesis must carry provenance back to a real claim.
    real_ids = {c["id"] for c in claims}
    for h in hypotheses:
        assert h.counters_canonical_id in real_ids
        assert h.rationale
        assert h.segment


def test_sub_gate_claims_from_the_real_ledger_are_excluded():
    claims = load_canonical_claims(GUSTO_CANONICAL)
    selected = {h.counters_canonical_id for h in build_hypotheses(claims)}
    weak = {c["id"] for c in claims if c["confidence"] < MIN_CONFIDENCE}
    assert weak  # the shipped ledger does contain sub-gate claims
    assert not (selected & weak)


def test_load_rejects_a_non_list_payload(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"canonical_claims": []}')
    with pytest.raises(ValueError, match="expected a list"):
        load_canonical_claims(bad)

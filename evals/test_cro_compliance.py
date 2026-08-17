"""CRO compliance gate — assertions in generated copy must trace to a usable source."""

from __future__ import annotations

from pathlib import Path

import pytest

from cro.compliance import (
    ComplianceReport,
    Fact,
    check_compliance,
    detect_assertions,
    load_facts,
)
from cro.models import Hypothesis, Variant
from cro.scoring import score_variant

FACTS_YAML = Path(__file__).resolve().parents[1] / "data" / "rippling_facts.yaml"


def _write_facts(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "facts.yaml"
    path.write_text(body)
    return path


def test_unverified_facts_are_excluded(tmp_path):
    # An allowlist that trusts its own unverified entries is not an allowlist.
    path = _write_facts(
        tmp_path,
        """
facts:
  - id: FACT-001
    statement: "Rippling includes MDM and SSO"
    category: differentiator
    source_url: "https://www.rippling.com/products/it/device-management"
    verified: true
  - id: FACT-002
    statement: "Rippling costs $8/user/month"
    category: pricing
    source_url: "https://www.rippling.com/blog/review"
    verified: false
""",
    )
    facts = load_facts(path)
    assert set(facts) == {"FACT-001"}
    assert isinstance(facts["FACT-001"], Fact)


def test_verified_fact_without_a_source_url_is_excluded(tmp_path):
    # verified: true with no URL is an unsourced claim wearing a badge.
    path = _write_facts(
        tmp_path,
        """
facts:
  - id: FACT-001
    statement: "Rippling scales to enterprise"
    category: differentiator
    source_url: ""
    verified: true
""",
    )
    assert load_facts(path) == {}


def test_missing_facts_key_raises(tmp_path):
    path = _write_facts(tmp_path, "other_key: []\n")
    with pytest.raises(ValueError, match="expected a top-level 'facts' list"):
        load_facts(path)


def test_reads_the_real_facts_file():
    # Startup config must parse. Count is not asserted — it changes as facts
    # get verified; what matters is that every returned fact is usable.
    facts = load_facts(FACTS_YAML)
    for fact in facts.values():
        assert fact.verified is True
        assert fact.source_url


# --- assertion detection -----------------------------------------------------


def test_comparative_language_needs_backing():
    found = detect_assertions("Faster than the alternative")
    assert [a.kind for a in found] == ["comparative"]
    assert found[0].trigger == "than"


def test_superlatives_need_backing():
    kinds = {a.kind for a in detect_assertions("The only platform that does this")}
    assert "comparative" in kinds


def test_competitor_names_need_backing():
    found = detect_assertions("Switching from Gusto is easy")
    assert any(a.kind == "competitor" and a.trigger == "gusto" for a in found)


def test_quantitative_claims_need_backing():
    found = detect_assertions("Run payroll across 50 states")
    assert any(a.kind == "quantitative" for a in found)


def test_plain_copy_asserts_nothing_requiring_backing():
    assert detect_assertions("Payroll that runs itself") == []


def test_detection_is_case_insensitive():
    assert detect_assertions("switching from GUSTO")


def test_each_trigger_is_reported_once():
    # "than ... than" is one comparative finding, not two — the report should
    # not inflate the unsourced count with duplicates.
    found = [
        a for a in detect_assertions("Cheaper than X and faster than Y") if a.kind == "comparative"
    ]
    assert len(found) == 1


# --- ref resolution ----------------------------------------------------------


LEDGER = {
    "CAN-009": "Gusto emphasizes simplicity, ease of use, and reliability",
    "CAN-008": "Gusto positions switching or migration from competitor HR software",
}
FACTS = {
    "FACT-004": Fact(
        id="FACT-004",
        statement="Rippling includes device management and single sign-on",
        source_url="https://www.rippling.com/products/it/device-management",
        verified=True,
    )
}


def _variant(headline: str, refs: list[str]) -> Variant:
    return Variant(
        id="VAR-001",
        hypothesis_id="HYP-001",
        headline=headline,
        subhead="",
        cta="See how",
        segment="30-200 employee migration",
        changed_elements=["hero"],
        claim_refs=refs,
        generated_by="sonnet-5/variant_gen_v1",
    )


def test_assertion_with_no_refs_at_all_is_unsourced():
    report = check_compliance(_variant("Simpler than Gusto", []), LEDGER, FACTS)
    assert report.compliant is False
    assert report.unsourced


def test_assertion_backed_by_a_relevant_ledger_claim_is_compliant():
    report = check_compliance(_variant("Simplicity that scales", ["CAN-009"]), LEDGER, FACTS)
    assert report.compliant is True
    assert report.unsourced == []


def test_assertion_unrelated_to_its_cited_ref_is_unsourced():
    # Cites a real claim, but the claim is about simplicity — it says nothing
    # about device management. This is the failure a naive "has a ref" check misses.
    report = check_compliance(_variant("Manage 5,000 devices", ["CAN-009"]), LEDGER, FACTS)
    assert report.compliant is False


def test_unknown_ref_is_reported_as_invalid():
    report = check_compliance(_variant("Simpler than Gusto", ["CAN-999"]), LEDGER, FACTS)
    assert "CAN-999" in report.invalid_refs
    assert report.compliant is False


def test_unverified_fact_id_is_invalid_because_load_facts_dropped_it():
    # FACT-003 is unverified, so it never entered the facts dict.
    report = check_compliance(_variant("Costs $8 per user", ["FACT-003"]), LEDGER, FACTS)
    assert "FACT-003" in report.invalid_refs
    assert report.compliant is False


def test_verified_fact_can_back_an_assertion():
    report = check_compliance(_variant("Device management built in", ["FACT-004"]), LEDGER, FACTS)
    assert report.compliant is True


def test_copy_asserting_nothing_needs_no_refs():
    report = check_compliance(_variant("Payroll that runs itself", []), LEDGER, FACTS)
    assert report.compliant is True
    assert report.assertions == []


def test_check_compliance_never_raises_on_garbage_input():
    report = check_compliance(_variant("", []), {}, {})
    assert isinstance(report, ComplianceReport)


def test_trace_names_the_heuristic():
    report = check_compliance(_variant("Manage 5,000 devices", ["CAN-009"]), LEDGER, FACTS)
    assert "overlap" in report.trace


# --- integration: compliance feeds the scoring gate --------------------------


HYP = Hypothesis(
    id="HYP-001",
    statement="Position Rippling as the platform you will not outgrow",
    counters_canonical_id="CAN-009",
    segment="30-200 employee migration",
    source_winning_score=80,
    rationale="Gusto's simplicity story breaks at multi-state scale",
)
CANONICAL = "Gusto emphasizes simplicity, ease of use, and reliability"


def test_noncompliant_variant_is_rejected_by_the_scoring_gate():
    variant = _variant("Cheaper than Gusto", [])
    report = check_compliance(variant, LEDGER, FACTS)
    scored = score_variant(variant, HYP, CANONICAL, unsourced_claims=report.unsourced)

    assert scored.shippable is False
    assert scored.reject_reason == "unsourced_claim"
    assert scored.score == 0


def test_compliant_variant_proceeds_to_normal_scoring():
    variant = _variant("Simplicity that survives 50 states", ["CAN-009"])
    report = check_compliance(variant, LEDGER, FACTS)
    scored = score_variant(variant, HYP, CANONICAL, unsourced_claims=report.unsourced)

    assert scored.reject_reason != "unsourced_claim"
    assert "specificity" in scored.trace

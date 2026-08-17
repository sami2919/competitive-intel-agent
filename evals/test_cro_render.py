"""CRO render — markdown brief, JSON payload, Optimizely export. Pure formatting."""

from __future__ import annotations

from datetime import UTC, datetime

from cro.models import Hypothesis, PageElement, PageSnapshot, ScoredVariant, Variant
from cro.render import render_json, render_markdown
from cro.testplan import build_test_plan

SNAPSHOT = PageSnapshot(
    url="https://www.rippling.com/payroll",
    fetched_at=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
    elements=[PageElement(role="hero", text="Payroll that runs itself")],
)
HYP = Hypothesis(
    id="HYP-001",
    statement="Counter-position against: Gusto emphasizes simplicity",
    counters_canonical_id="CAN-009",
    segment="30-200 employee migration",
    source_winning_score=80,
    rationale="CAN-009 is a cross_channel_winner at 80/100",
)
PLAN = build_test_plan(2, baseline_cvr=0.03, weekly_sessions=100_000)


def _scored(vid: str, score: int, shippable: bool, reason=None) -> ScoredVariant:
    return ScoredVariant(
        variant=Variant(
            id=vid,
            hypothesis_id="HYP-001",
            headline=f"Headline for {vid}",
            subhead="A subhead.",
            cta="See how",
            segment="30-200 employee migration",
            changed_elements=["hero"],
            claim_refs=["CAN-009"],
            generated_by="sonnet-5/variant_gen_v1",
        ),
        score=score,
        trace=f"trace for {vid}",
        shippable=shippable,
        reject_reason=reason,
    )


SHIPPED = _scored("VAR-001", 85, True)
REJECTED = _scored("VAR-002", 0, False, "unsourced_claim")


def test_markdown_separates_ship_list_from_rejected():
    md = render_markdown(SNAPSHOT, [HYP], [SHIPPED, REJECTED], PLAN)
    ship_at = md.index("## Ship")
    reject_at = md.index("## Rejected")
    assert ship_at < reject_at
    assert md.index("VAR-001") < reject_at
    assert md.index("VAR-002") > reject_at


def test_markdown_states_the_reject_reason():
    md = render_markdown(SNAPSHOT, [HYP], [SHIPPED, REJECTED], PLAN)
    assert "unsourced_claim" in md


def test_markdown_carries_provenance_back_to_a_claim_id():
    md = render_markdown(SNAPSHOT, [HYP], [SHIPPED], PLAN)
    assert "CAN-009" in md
    assert "HYP-001" in md


def test_markdown_includes_the_test_plan_verdict():
    md = render_markdown(SNAPSHOT, [HYP], [SHIPPED], PLAN)
    assert "runnable" in md
    assert str(PLAN.required_per_arm) in md.replace(",", "")


def test_markdown_stamps_the_page_and_fetch_date():
    md = render_markdown(SNAPSHOT, [HYP], [SHIPPED], PLAN)
    assert "https://www.rippling.com/payroll" in md
    assert "2026-07-26" in md


def test_markdown_shows_every_score_trace_for_auditability():
    md = render_markdown(SNAPSHOT, [HYP], [SHIPPED, REJECTED], PLAN)
    assert "trace for VAR-001" in md
    assert "trace for VAR-002" in md


def test_markdown_handles_a_run_with_no_shippable_variants():
    md = render_markdown(SNAPSHOT, [HYP], [REJECTED], PLAN)
    assert "No variants passed" in md


def test_json_has_the_expected_top_level_keys():
    payload = render_json(SNAPSHOT, [HYP], [SHIPPED, REJECTED], PLAN)
    assert set(payload) == {"page", "generated_at", "test_plan", "hypotheses", "variants"}


def test_json_records_shippable_and_rejected_alike():
    payload = render_json(SNAPSHOT, [HYP], [SHIPPED, REJECTED], PLAN)
    assert len(payload["variants"]) == 2
    assert {v["shippable"] for v in payload["variants"]} == {True, False}


def test_json_is_serializable():
    import json

    payload = render_json(SNAPSHOT, [HYP], [SHIPPED], PLAN)
    assert json.loads(json.dumps(payload))["page"] == SNAPSHOT.url


# --- optimizely export, file writing, end-to-end -----------------------------


import json as _json  # noqa: E402
from pathlib import Path  # noqa: E402

from cro.compliance import Fact, check_compliance  # noqa: E402
from cro.generate import generate_variants  # noqa: E402
from cro.render import render_optimizely, write_outputs  # noqa: E402
from cro.scoring import score_variant  # noqa: E402
from cro.snapshot import parse_snapshot  # noqa: E402


def test_optimizely_payload_includes_control_plus_each_variant():
    payload = render_optimizely(SNAPSHOT, [SHIPPED])
    names = [v["name"] for v in payload["variations"]]
    assert names[0] == "control"
    assert "VAR-001" in names


def test_optimizely_traffic_splits_as_evenly_as_integers_allow():
    # 3 arms cannot have equal integer weights summing to 100, so the invariant is
    # "sums to 100 and no arm differs from another by more than 1" — the remainder
    # rides on control. An exactly-equal assertion would be unsatisfiable here.
    payload = render_optimizely(SNAPSHOT, [SHIPPED, _scored("VAR-003", 70, True)])
    weights = [v["weight"] for v in payload["variations"]]
    assert sum(weights) == 100
    assert max(weights) - min(weights) <= 1


def test_optimizely_splits_exactly_when_arms_divide_evenly():
    payload = render_optimizely(SNAPSHOT, [SHIPPED])
    weights = [v["weight"] for v in payload["variations"]]
    assert weights == [50, 50]


def test_optimizely_excludes_rejected_variants():
    payload = render_optimizely(SNAPSHOT, [SHIPPED])
    assert all(v["name"] != "VAR-002" for v in payload["variations"])


def test_optimizely_payload_is_serializable_and_names_the_page():
    payload = render_optimizely(SNAPSHOT, [SHIPPED])
    assert _json.loads(_json.dumps(payload))["page_url"] == SNAPSHOT.url


def test_write_outputs_creates_all_three_files(tmp_path):
    paths = write_outputs(
        "rippling-payroll",
        markdown="# brief",
        payload={"page": "x"},
        optimizely={"page_url": "x"},
        out_dir=tmp_path,
    )
    assert set(paths) == {"markdown", "json", "optimizely"}
    for path in paths.values():
        assert Path(path).exists()
    assert (tmp_path / "rippling-payroll_variants.md").read_text() == "# brief"


def test_end_to_end_offline_pipeline(tmp_path):
    """snapshot -> generate (stubbed) -> compliance -> score -> render, no network."""
    page = (
        "# Payroll that runs itself\n\n"
        "Run payroll in 90 seconds across all 50 states.\n\n"
        "[Get started](https://www.rippling.com/signup)\n"
    )
    snapshot = parse_snapshot("https://www.rippling.com/payroll", page)

    ledger = {"CAN-009": "Gusto emphasizes simplicity, ease of use, and reliability"}
    facts = {
        "FACT-005": Fact(
            id="FACT-005",
            statement="Rippling supports multi-state US payroll across 50 states",
            source_url="https://www.rippling.com/payroll",
            verified=True,
        )
    }
    allowed = {**ledger, **{k: f.statement for k, f in facts.items()}}

    model_output = _json.dumps(
        [
            {
                "headline": "Simplicity that survives 50 states",
                "subhead": "Run payroll across every US state.",
                "cta": "See how",
                "changed_elements": ["hero"],
                "claim_refs": ["FACT-005"],
                "segment": "30-200 employee migration",
            },
            {
                "headline": "Cheaper than Gusto",
                "subhead": "",
                "cta": "See how",
                "changed_elements": ["hero"],
                "claim_refs": [],
                "segment": "30-200 employee migration",
            },
        ]
    )

    class _Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            from evals.stub import StubResponse, StubUsage, TextBlock

            return StubResponse(content=[TextBlock(text=model_output)], usage=StubUsage())

    variants, _usage = generate_variants(snapshot, HYP, allowed, 2, _Client(), "PROMPT")
    assert len(variants) == 2

    scored = []
    for variant in variants:
        report = check_compliance(variant, ledger, facts)
        scored.append(
            score_variant(
                variant, HYP, "Gusto emphasizes simplicity", unsourced_claims=report.unsourced
            )
        )

    # The unsourced comparative variant must not survive.
    assert any(s.shippable for s in scored)
    assert any(s.reject_reason == "unsourced_claim" for s in scored)

    plan = build_test_plan(len([s for s in scored if s.shippable]), 0.03, 100_000)
    paths = write_outputs(
        "rippling-payroll",
        render_markdown(snapshot, [HYP], scored, plan),
        render_json(snapshot, [HYP], scored, plan),
        render_optimizely(snapshot, [s for s in scored if s.shippable]),
        out_dir=tmp_path,
    )
    brief = Path(paths["markdown"]).read_text()
    assert "## Ship" in brief
    assert "unsourced_claim" in brief

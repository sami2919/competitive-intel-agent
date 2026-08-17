"""CRO variant generation — constrained Sonnet call, tolerant parsing, never raises."""

from __future__ import annotations

from agent.llm import load_prompt


def test_prompt_loads_and_strips_its_metadata_block():
    prompt = load_prompt("variant_gen_v1")
    # load_prompt only strips <!-- --> when it is the FIRST content in the file.
    assert "Prompt version:" not in prompt
    assert prompt.strip()


def test_prompt_states_the_hard_constraints():
    prompt = load_prompt("variant_gen_v1").lower()
    # These four rules are what keep the deterministic gates satisfiable.
    assert "one element" in prompt
    assert "claim_refs" in prompt
    assert "json" in prompt
    assert "do not invent" in prompt


# --- generation --------------------------------------------------------------


import json  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

from cro.generate import (  # noqa: E402
    GENERATOR_MODEL,
    GENERATOR_PROMPT_VERSION,
    build_user_message,
    generate_variants,
)
from cro.models import Hypothesis, PageElement, PageSnapshot  # noqa: E402
from evals.stub import StubResponse, StubUsage, TextBlock  # noqa: E402

SNAPSHOT = PageSnapshot(
    url="https://www.rippling.com/payroll",
    fetched_at=datetime.now(UTC),
    elements=[
        PageElement(role="hero", text="Payroll that runs itself"),
        PageElement(role="subhead", text="Run payroll in 90 seconds."),
        PageElement(role="cta", text="Get started"),
    ],
)
HYP = Hypothesis(
    id="HYP-001",
    statement="Counter-position against: Gusto emphasizes simplicity",
    counters_canonical_id="CAN-009",
    segment="30-200 employee migration",
    source_winning_score=80,
    rationale="CAN-009 is a cross_channel_winner at 80/100",
)
ALLOWED = {"CAN-009": "Gusto emphasizes simplicity, ease of use, and reliability"}

GOOD_JSON = json.dumps(
    [
        {
            "headline": "Simplicity that survives 50 states",
            "subhead": "Run payroll across every US jurisdiction.",
            "cta": "See how",
            "changed_elements": ["hero"],
            "claim_refs": ["CAN-009"],
            "segment": "30-200 employee migration",
        }
    ]
)


class _OneShotClient:
    """Minimal stand-in: returns one scripted response and records the call."""

    def __init__(self, text: str) -> None:
        self.messages = self
        self._text = text
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return StubResponse(content=[TextBlock(text=self._text)], usage=StubUsage())


def test_user_message_carries_control_hypothesis_and_allowed_refs():
    msg = build_user_message(SNAPSHOT, HYP, ALLOWED, n_variants=2)
    assert "Payroll that runs itself" in msg
    assert "CAN-009" in msg
    assert "30-200 employee migration" in msg
    assert "2" in msg


def test_user_message_never_leaks_a_ref_outside_the_allowed_set():
    msg = build_user_message(SNAPSHOT, HYP, ALLOWED, n_variants=1)
    assert "FACT-003" not in msg


def test_generates_variants_with_provenance():
    client = _OneShotClient(GOOD_JSON)
    variants, usage = generate_variants(SNAPSHOT, HYP, ALLOWED, 1, client, "PROMPT")

    assert len(variants) == 1
    v = variants[0]
    assert v.id == "VAR-001"
    assert v.hypothesis_id == "HYP-001"
    assert v.headline == "Simplicity that survives 50 states"
    assert v.claim_refs == ["CAN-009"]
    assert v.generated_by == f"sonnet-5/{GENERATOR_PROMPT_VERSION}"
    assert usage.output_tokens > 0


def test_uses_the_generator_model_and_the_supplied_prompt():
    client = _OneShotClient(GOOD_JSON)
    generate_variants(SNAPSHOT, HYP, ALLOWED, 1, client, "PROMPT")
    call = client.calls[0]
    assert call["model"] == GENERATOR_MODEL
    assert call["system"] == "PROMPT"


def test_variant_ids_are_sequential():
    two = json.dumps(
        [
            {"headline": "A headline here", "changed_elements": ["hero"], "claim_refs": []},
            {"headline": "B headline here", "changed_elements": ["cta"], "claim_refs": []},
        ]
    )
    variants, _ = generate_variants(SNAPSHOT, HYP, ALLOWED, 2, _OneShotClient(two), "P")
    assert [v.id for v in variants] == ["VAR-001", "VAR-002"]


def test_prose_around_the_json_is_tolerated():
    noisy = f"Here are your variants:\n{GOOD_JSON}\nHope that helps!"
    variants, _ = generate_variants(SNAPSHOT, HYP, ALLOWED, 1, _OneShotClient(noisy), "P")
    assert len(variants) == 1


def test_malformed_json_returns_empty_and_never_raises():
    variants, usage = generate_variants(SNAPSHOT, HYP, ALLOWED, 1, _OneShotClient("not json"), "P")
    assert variants == []
    assert usage.output_tokens > 0


def test_entries_without_a_headline_are_dropped():
    partial = json.dumps(
        [
            {"subhead": "no headline here", "changed_elements": ["hero"]},
            {"headline": "A real headline", "changed_elements": ["hero"], "claim_refs": []},
        ]
    )
    variants, _ = generate_variants(SNAPSHOT, HYP, ALLOWED, 2, _OneShotClient(partial), "P")
    assert [v.headline for v in variants] == ["A real headline"]


def test_missing_changed_elements_defaults_to_hero():
    entry = json.dumps([{"headline": "A real headline", "claim_refs": []}])
    variants, _ = generate_variants(SNAPSHOT, HYP, ALLOWED, 1, _OneShotClient(entry), "P")
    assert variants[0].changed_elements == ["hero"]


def test_segment_defaults_to_the_hypothesis_segment():
    entry = json.dumps([{"headline": "A real headline", "changed_elements": ["hero"]}])
    variants, _ = generate_variants(SNAPSHOT, HYP, ALLOWED, 1, _OneShotClient(entry), "P")
    assert variants[0].segment == HYP.segment

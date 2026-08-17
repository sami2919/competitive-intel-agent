"""Render scored variants as a brief, a JSON payload, and an Optimizely export.

Pure formatting — no LLM, no network. Every number shown here was computed by
cro/scoring.py or cro/testplan.py, so the brief is a view over deterministic
results rather than a second place where judgment happens.

Rejected variants are ALWAYS shown with their reason. A CRO tool that silently
drops what it refused is not auditable, and the refusals are the most interesting
part of the output.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cro.models import Hypothesis, PageSnapshot, ScoredVariant
from cro.testplan import TestPlan


def render_markdown(
    snapshot: PageSnapshot,
    hypotheses: list[Hypothesis],
    scored: list[ScoredVariant],
    plan: TestPlan,
) -> str:
    """The human-readable brief."""
    shipped = [s for s in scored if s.shippable]
    rejected = [s for s in scored if not s.shippable]
    by_id = {h.id: h for h in hypotheses}

    lines = [
        f"# Landing page variants — {snapshot.url}",
        "",
        f"Page snapshot: {snapshot.fetched_at.date().isoformat()} · "
        f"generated {datetime.now(UTC).date().isoformat()}",
        "",
        "## Test plan",
        "",
        plan.trace,
        "",
        "## Ship",
        "",
    ]
    if shipped:
        for item in shipped:
            lines += _variant_block(item, by_id.get(item.variant.hypothesis_id))
    else:
        lines += ["No variants passed the gate. See Rejected below for why.", ""]

    lines += ["## Rejected", ""]
    if rejected:
        for item in rejected:
            reason = item.reject_reason or "below_gate"
            lines += [
                f'- **{item.variant.id}** ({reason}) — "{item.variant.headline}"',
                f"  - {item.trace}",
            ]
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def render_json(
    snapshot: PageSnapshot,
    hypotheses: list[Hypothesis],
    scored: list[ScoredVariant],
    plan: TestPlan,
) -> dict[str, Any]:
    """The machine-readable payload. Mirrors the markdown, loses nothing."""
    return {
        "page": snapshot.url,
        "generated_at": datetime.now(UTC).isoformat(),
        "test_plan": plan.model_dump(),
        "hypotheses": [h.model_dump() for h in hypotheses],
        "variants": [
            {
                **item.variant.model_dump(),
                "score": item.score,
                "trace": item.trace,
                "shippable": item.shippable,
                "reject_reason": item.reject_reason,
            }
            for item in scored
        ],
    }


def _variant_block(item: ScoredVariant, hypothesis: Hypothesis | None) -> list[str]:
    variant = item.variant
    provenance = (
        f"{hypothesis.id} → counters {hypothesis.counters_canonical_id} "
        f"({hypothesis.source_winning_score}/100)"
        if hypothesis
        else variant.hypothesis_id
    )
    block = [
        f"### {variant.id} — {item.score}/100",
        "",
        f"- **Headline:** {variant.headline}",
    ]
    if variant.subhead:
        block.append(f"- **Subhead:** {variant.subhead}")
    if variant.cta:
        block.append(f"- **CTA:** {variant.cta}")
    block += [
        f"- **Changes:** {', '.join(variant.changed_elements)}",
        f"- **Segment:** {variant.segment}",
        f"- **Cites:** {', '.join(variant.claim_refs) or '(none needed)'}",
        f"- **Provenance:** {provenance}",
        f"- **Score trace:** {item.trace}",
        "",
    ]
    return block


def render_optimizely(snapshot: PageSnapshot, shipped: list[ScoredVariant]) -> dict[str, Any]:
    """Experiment payload shaped for an Optimizely Web import.

    Deliberately a plain, documented dict rather than a claimed-exact API schema —
    the field names mirror Optimizely's experiment/variation vocabulary so a growth
    engineer can map it, but this has not been validated against a live account.
    Traffic splits evenly across control + variants, matching the sizing assumption
    in cro/testplan.py; an uneven split would invalidate the day estimate.
    """
    n_arms = len(shipped) + 1
    base = 100 // n_arms
    weights = [base] * n_arms
    weights[0] += 100 - sum(weights)  # remainder rides on control

    variations: list[dict[str, Any]] = [{"name": "control", "weight": weights[0], "changes": []}]
    for item, weight in zip(shipped, weights[1:], strict=True):
        variant = item.variant
        variations.append(
            {
                "name": variant.id,
                "weight": weight,
                "changes": [
                    {"selector": role, "value": _text_for(variant, role)}
                    for role in variant.changed_elements
                ],
                "metadata": {
                    "hypothesis_id": variant.hypothesis_id,
                    "claim_refs": variant.claim_refs,
                    "cro_score": item.score,
                },
            }
        )

    return {
        "page_url": snapshot.url,
        "experiment_name": f"CRO — {snapshot.url.rstrip('/').split('/')[-1] or 'home'}",
        "variations": variations,
    }


def write_outputs(
    slug: str,
    markdown: str,
    payload: dict[str, Any],
    optimizely: dict[str, Any],
    out_dir: str | Path = "outputs/cro",
) -> dict[str, Path]:
    """Write the three artifacts. Returns {kind: path}."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": directory / f"{slug}_variants.md",
        "json": directory / f"{slug}_variants.json",
        "optimizely": directory / f"{slug}_optimizely.json",
    }
    paths["markdown"].write_text(markdown, encoding="utf-8")
    paths["json"].write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    paths["optimizely"].write_text(json.dumps(optimizely, indent=2, default=str), encoding="utf-8")
    return paths


def _text_for(variant: Any, role: str) -> str:
    return {"hero": variant.headline, "subhead": variant.subhead, "cta": variant.cta}.get(role, "")

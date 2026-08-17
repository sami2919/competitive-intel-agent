"""Deterministic confidence-by-source summary (Phase 6, Step 4).

No LLM call. Same "compute in Python, append post-generation" pattern already used
for the grounding-warning text — zero hallucination risk, no grounding validation
needed since nothing here is model-generated prose.
"""

from __future__ import annotations

from collections import defaultdict

from ledger.confidence import PRIMARY_SOURCES, SECONDARY_SOURCES, source_kind
from ledger.models import CONFIDENCE_GATE, Claim


def format_confidence_by_source(claims: list[Claim]) -> str:
    """Markdown table: source tool -> claim count -> avg confidence -> primary/secondary.

    Claims with no recorded source_tool (e.g. hand-built in tests) are skipped —
    there is nothing meaningful to group them under.
    """
    by_tool: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        if claim.source_tool:
            by_tool[claim.source_tool].append(claim)

    if not by_tool:
        return "## Confidence by Source\n\n(no sourced claims to summarize)"

    lines = [
        "## Confidence by Source",
        "",
        "| Source | Claims | Avg Confidence | Kind |",
        "|---|---|---|---|",
    ]
    for tool in sorted(by_tool):
        group = by_tool[tool]
        avg_confidence = sum(c.confidence for c in group) / len(group)
        lines.append(f"| {tool} | {len(group)} | {avg_confidence:.2f} | {source_kind(tool)} |")
    return "\n".join(lines)


def format_evidence_quality(claims: list[Claim]) -> str:
    """Deterministic primary-vs-secondary breakdown so a reviewer can see which
    conclusions rest on owned-site/ad-library evidence vs secondary reporting.

    No LLM call. Splits sourced claims by ``source_kind`` and by the confidence gate,
    so the reviewer sees both the *kind* of evidence and how much of it is body-eligible.
    """
    primary = [c for c in claims if c.source_tool and source_kind(c.source_tool) == "primary"]
    secondary = [c for c in claims if c.source_tool and source_kind(c.source_tool) == "secondary"]
    body_eligible = [c for c in claims if c.confidence >= CONFIDENCE_GATE]
    hypothesis_only = [c for c in claims if c.confidence < CONFIDENCE_GATE]

    def _avg(group: list[Claim]) -> str:
        return f"{sum(c.confidence for c in group) / len(group):.2f}" if group else "n/a"

    primary_tools = ", ".join(sorted(PRIMARY_SOURCES))
    secondary_tools = ", ".join(sorted(SECONDARY_SOURCES))
    return "\n".join(
        [
            "## Evidence quality",
            "",
            "| Kind | Sources | Claims | Avg confidence |",
            "|---|---|---|---|",
            f"| Primary (owned site / ad library / social / wayback) | {primary_tools} | {len(primary)} | {_avg(primary)} |",
            f"| Secondary (press / reviews / jobs) | {secondary_tools} | {len(secondary)} | {_avg(secondary)} |",
            "",
            f"Body-eligible (confidence >= {CONFIDENCE_GATE}): {len(body_eligible)} of {len(claims)}. "
            f"Hypothesis-only (confidence < {CONFIDENCE_GATE}): {len(hypothesis_only)}.",
        ]
    )


def format_how_to_read() -> str:
    """Deterministic plain-English explainer for the brief's decision logic + the
    grounding guarantee. Static text — the rules are fixed by the prompt + validator,
    so this can be computed without the ledger. Appended so a reviewer reads the brief
    knowing exactly what 'winning' / 'test' / 'unverified' mean and how validation works.
    """
    return "\n".join(
        [
            "## How to read this brief",
            "",
            "- **Verdict** — BLUF (bottom line up front): the 3-5 key judgments, each with",
            "  a confidence label and a concrete '→ For Rippling:' action. If a judgment",
            "  implies no action, it doesn't appear.",
            "- **Strategy in plain English** — a one-paragraph synthesis of the competitor's",
            "  current strategy, restating only body-eligible (confidence >= 0.5) claims with",
            "  citations. Synthesis, not new evidence.",
            "- **What's Winning** — claims that persist (an ad running 90+ days) or are",
            "  corroborated across 2+ independent channels. Each carries a deterministic",
            "  `winning=NN/100` score (corroboration 40 + persistence 30 + recency 30) and",
            "  ends with a 'why it likely persists' clause tagged as inference, not measured.",
            "- **What Looks Like a Test** — ads under 90 days old and single-channel.",
            "  These are HYPOTHESES ('may be testing...'), not conclusions.",
            "- **What Changed Recently** — strategic shifts only (positioning, pricing,",
            "  ICP, messaging, leadership/funding), not product-launch inventory.",
            "- **The three-way distinction** — strategic shift (changes who they target or",
            "  how they frame themselves) vs product launch (new feature in the existing",
            "  story, relegated to a supporting note) vs marketing test (new, single-channel",
            "  ad variant, belongs in 'What Looks Like a Test'). Segment moves are labeled",
            "  new-ICP vs extension-of-existing-motion; press-driven shifts are flagged as",
            "  'appears to / directionally', not settled fact.",
            "- **Unverified signals** — confidence below 0.5. Raw signal; never treated as fact.",
            "- **Rippling-relevance** — a battlecard: where they win / where we win (honest",
            "  concessions included), landmines (discovery questions), objection handling,",
            "  and ranked campaign angles — each backed only by confidence >= 0.5 evidence",
            "  and tagged with its target segment.",
            "- **Campaign test hypotheses** — ranked 'we believe X because [evidence]' bets",
            "  for Rippling's growth team, derived from ad longevity/corroboration signals.",
            "  Hypotheses to test, never conclusions.",
            "- **Confidence labels** — deterministic ICD-203-style wording mapped from the",
            "  rubric: high (2+ independent sources) / moderate (single primary) /",
            "  moderate-low (single secondary) / low (inferred). Ad longevity labels:",
            "  proven (90d+) / sustained (45-89d) / maturing (14-44d) / just launched (<14d).",
            "",
            "Grounding: every `[CLM-xxx]` / `[CAN-xxx]` citation maps to a real ledger claim",
            "(no hallucinated IDs). Sub-gate (confidence < 0.5) claims appear only in",
            "'What Looks Like a Test', 'Campaign test hypotheses', or 'Unverified signals',",
            "framed as hypotheses; body-eligible (>= 0.5) claims may NOT hide in the",
            "Unverified appendix. Validation is deterministic Python, not model self-report.",
        ]
    )

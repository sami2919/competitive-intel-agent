"""Variant generation — ONE Sonnet call per hypothesis, constrained to allowed citations.

The model never sees raw crawled pages. It sees the page snapshot (the control arm) and
the allowed citation set — statements the ledger already evidenced, plus verified
first-party facts. That is DRY(E) applied to a second agent: the hard extraction
reasoning happened once, at ingestion, and this step reads the result.

Parsing is tolerant and NEVER raises. A model that returns prose, malformed JSON, or an
empty array yields an empty variant list, which the caller reports as a skipped
hypothesis rather than a crash.
"""

from __future__ import annotations

import json
from typing import Any

from agent.cost import Usage
from cro.models import ElementRole, Hypothesis, PageSnapshot, Variant

GENERATOR_MODEL = "claude-sonnet-5"
GENERATOR_PROMPT_VERSION = "variant_gen_v1"
MAX_TOKENS = 2048

_VALID_ROLES: tuple[ElementRole, ...] = ("hero", "subhead", "cta", "offer", "proof")


def build_user_message(
    snapshot: PageSnapshot,
    hypothesis: Hypothesis,
    allowed_refs: dict[str, str],
    n_variants: int,
) -> str:
    """Assemble the CONTROL / HYPOTHESIS / ALLOWED CITATIONS payload."""
    control = "\n".join(f"- {element.role}: {element.text}" for element in snapshot.elements)
    citations = "\n".join(f"- {ref_id}: {statement}" for ref_id, statement in allowed_refs.items())
    return (
        f"CONTROL (live page {snapshot.url}):\n{control}\n\n"
        f"HYPOTHESIS {hypothesis.id}: {hypothesis.statement}\n"
        f"Counters: {hypothesis.counters_canonical_id}\n"
        f"Segment: {hypothesis.segment}\n"
        f"Why: {hypothesis.rationale}\n\n"
        f"ALLOWED CITATIONS (the only facts you may assert):\n{citations or '- (none)'}\n\n"
        f"Produce {n_variants} variant(s) as a JSON array."
    )


def generate_variants(
    snapshot: PageSnapshot,
    hypothesis: Hypothesis,
    allowed_refs: dict[str, str],
    n_variants: int,
    client: Any,
    prompt: str,
    start_index: int = 1,
) -> tuple[list[Variant], Usage]:
    """One model call -> unscored Variants. Never raises.

    `start_index` continues VAR-xxx numbering across hypotheses. Without it every
    hypothesis restarts at VAR-001, which collides ids inside a single run and puts
    duplicate variation names in the Optimizely payload (caught in a live run).
    """
    response = client.messages.create(
        model=GENERATOR_MODEL,
        max_tokens=MAX_TOKENS,
        system=prompt,
        messages=[
            {
                "role": "user",
                "content": build_user_message(snapshot, hypothesis, allowed_refs, n_variants),
            }
        ],
    )
    usage = Usage.from_sdk(response.usage)
    text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            break
    return _parse_variants(text, hypothesis, start_index), usage


def _parse_variants(text: str, hypothesis: Hypothesis, start_index: int = 1) -> list[Variant]:
    """Pull the JSON array out of the response and build Variants. Drops bad entries."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        raw = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []

    variants: list[Variant] = []
    for entry in raw:
        if not isinstance(entry, dict) or not str(entry.get("headline", "")).strip():
            continue
        variants.append(
            Variant(
                id=f"VAR-{start_index + len(variants):03d}",
                hypothesis_id=hypothesis.id,
                headline=str(entry["headline"]).strip(),
                subhead=str(entry.get("subhead", "")).strip(),
                cta=str(entry.get("cta", "")).strip(),
                segment=str(entry.get("segment") or hypothesis.segment),
                changed_elements=_roles(entry.get("changed_elements")),
                claim_refs=[str(r) for r in entry.get("claim_refs", []) if r],
                generated_by=f"sonnet-5/{GENERATOR_PROMPT_VERSION}",
            )
        )
    return variants


def _roles(value: Any) -> list[ElementRole]:
    """Keep only recognised element roles; default to hero when none survive.

    Defaulting rather than dropping is deliberate: a variant with an unparseable
    changed_elements still gets scored, and the multivariate gate still protects us.
    """
    if not isinstance(value, list):
        return ["hero"]
    roles = [r for r in value if r in _VALID_ROLES]
    return roles or ["hero"]

"""Haiku-assisted claim clustering for the 0.9 corroboration tier (eng review D2).

Per-source extraction produces separate Claim objects for the same assertion found on
different channels (e.g. homepage AND Meta ads). Clustering groups same-statement
claims across independent sources so the 0.9 corroboration tier fires when two+
channels agree on the same angle.

Usage:
    from ledger.clustering import cluster_claims
    from agent.llm import load_prompt

    prompt = load_prompt("clustering_v1")
    canonical_claims, usage = cluster_claims(all_claims, client, prompt)
    # canonical_claims replaces un-grouped entries in the final ledger
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from agent.cost import Usage
from ledger.models import CanonicalClaim, Claim, Evidence
from ledger.signal import classify_corroboration_signal, winning_score

CLUSTERING_MODEL = "claude-haiku-4-5"
CLUSTERING_PROMPT_VERSION = "clustering_v1"


def cluster_claims(
    claims: list[Claim], client: Any, prompt: str, *, now: datetime | None = None
) -> tuple[list[CanonicalClaim], Usage]:
    """Group same-assertion claims across independent sources via Haiku.

    Args:
        claims: Every Claim extracted for this competitor (from all source tools).
        client: An anthropic.Anthropic client (or StubClient in tests).
        prompt: The clustering prompt body (load via load_prompt("clustering_v1")).

    Returns:
        (canonical_claims, usage). Singleton claims (not grouped) are omitted
        and remain as individual Claims in the ledger. Only merged groups produce
        a CanonicalClaim.
    """
    claim_map: dict[str, Claim] = {c.id: c for c in claims}

    # Format input: one line per claim so Haiku can reference each by ID.
    input_lines = "\n".join(f"[{c.id}] ({c.category}) {c.statement}" for c in claims)

    resp = client.messages.create(
        model=CLUSTERING_MODEL,
        max_tokens=4096,
        system=prompt,
        messages=[{"role": "user", "content": input_lines}],
    )
    usage = Usage.from_sdk(resp.usage)

    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            break

    groups = _parse_groups(text)

    canonical_claims: list[CanonicalClaim] = []
    counter = 0

    for group in groups:
        raw_ids = group.get("member_claim_ids")
        # Defensive: skip non-list / single-element groups (must be 2+ per spec).
        if not isinstance(raw_ids, list) or len(raw_ids) < 2:
            continue
        # Defensive: drop groups referencing unknown claim IDs (never raise).
        if any(cid not in claim_map for cid in raw_ids):
            continue

        member_claims = [claim_map[cid] for cid in raw_ids]

        # Category = most common among members (ties break to first encountered).
        cat_counts: Counter[str] = Counter(c.category for c in member_claims)
        category = cat_counts.most_common(1)[0][0]

        # Evidence = union of member evidence, deduplicated by source_url.
        evidence_map: dict[str, Evidence] = {}
        for c in member_claims:
            for e in c.evidence:
                if e.source_url not in evidence_map:
                    evidence_map[e.source_url] = e
        evidence = list(evidence_map.values())

        # Confidence: deterministic rubric from the evidence union.
        n_independent = len(evidence)
        if n_independent >= 2:
            confidence = 0.9
            trace = f"two-or-more independent sources agree 0.9 ({n_independent} sources)"
        else:
            max_conf = max(c.confidence for c in member_claims)
            confidence = max_conf
            trace = f"single source, kept at {max_conf}"

        signal, signal_trace = classify_corroboration_signal(category, n_independent)

        counter += 1
        canonical = CanonicalClaim(
            id=f"CAN-{counter:03d}",
            competitor=member_claims[0].competitor,
            category=category,
            canonical_statement=group.get("canonical_statement", member_claims[0].statement),
            member_claim_ids=sorted(raw_ids),
            evidence=evidence,
            confidence=confidence,
            confidence_trace=trace,
            signal=signal,
            signal_trace=signal_trace,
        )
        # Frozen model — set the deterministic winning score via copy (computed from
        # the evidence + independent source count; inject `now` for deterministic tests).
        score, score_trace = winning_score(canonical, now or datetime.now(UTC))
        canonical_claims.append(
            canonical.model_copy(
                update={"winning_score": score, "winning_score_trace": score_trace}
            )
        )

    return canonical_claims, usage


def _parse_groups(text: str) -> list[dict]:
    """Pull the JSON array out of the model response (it may have surrounding prose).

    Mirrors extractor._parse_claims: find first '[' ... last ']', parse JSON,
    return valid group dicts. Never raises.
    """
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [g for g in data if isinstance(g, dict) and g.get("member_claim_ids")]

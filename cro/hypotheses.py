"""Competitor ledger -> ranked test hypotheses. Deterministic; the model never picks.

Hypotheses are SELECTED from CanonicalClaims that already cleared the ledger's
corroboration tier, not invented. That provenance is the whole point: in a readout you
can answer "why are we testing this?" with a claim id, its evidence, and its winning
score, rather than "the model suggested it."

Ranking, in order:
  1. signal      cross_channel_winner > durable_pillar > unsignalled.
                 A proven paid angle is worth more to counter than table stakes — the
                 competitor is spending money to defend it.
  2. winning_score  deterministic 0-100 from the ledger.
  3. confidence     tiebreak.

Only claims at confidence >= MIN_CONFIDENCE are eligible. A test built on a 0.5 claim is
a test built on a single unverified source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cro.models import Hypothesis

# Matches the ledger's "single primary source" tier. Below this, a claim is one
# secondary mention — not enough to spend a test slot on.
MIN_CONFIDENCE = 0.7

_SIGNAL_RANK = {"cross_channel_winner": 2, "durable_pillar": 1}

# Where a competitor claim is contestable. Ordered — first match wins, so the more
# specific segments must precede the general ones.
_SEGMENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("solopreneurs", ("solopreneur", "s-corp", "solo", "freelanc")),
    ("global / multi-entity", ("international", "global", "country", "countries", "contractor")),
    ("enterprise", ("enterprise", "large business")),
    (
        "30-200 employee migration",
        # "simplif" not "simplicity" — the shipped Gusto ledger's top claim says
        # "simplifying", and matching the noun only sent the highest-value
        # hypothesis to the default segment.
        ("switch", "migrat", "outgrow", "scale", "growing", "simplif", "ease of use"),
    ),
    ("multi-state US", ("state", "multi-state", "jurisdiction")),
)
_DEFAULT_SEGMENT = "SMB core"


def load_canonical_claims(path: str | Path) -> list[dict[str, Any]]:
    """Read a shipped {competitor}_canonical.json. Raises if it is not a claim list."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list of canonical claims, got {type(data).__name__}")
    return data


def build_hypotheses(
    claims: list[dict[str, Any]],
    limit: int | None = None,
    min_confidence: float = MIN_CONFIDENCE,
) -> list[Hypothesis]:
    """Rank eligible claims and turn the top ones into test hypotheses."""
    eligible = [c for c in claims if c.get("confidence", 0.0) >= min_confidence]
    ranked = sorted(eligible, key=_rank_key, reverse=True)
    selected = ranked[:limit] if limit else ranked
    return [_to_hypothesis(claim, i) for i, claim in enumerate(selected, start=1)]


def infer_segment(statement: str) -> tuple[str, str]:
    """Segment this claim is contestable in, plus the term that decided it.

    Keyword rules, not a model call — so the segment on every hypothesis is explainable
    and stable across runs. Returns (segment, trace).
    """
    low = statement.lower()
    for segment, keywords in _SEGMENT_RULES:
        for keyword in keywords:
            if keyword in low:
                return segment, f"matched '{keyword}'"
    return _DEFAULT_SEGMENT, "no segment keyword matched — defaulted"


def _rank_key(claim: dict[str, Any]) -> tuple[int, int, float]:
    return (
        _SIGNAL_RANK.get(claim.get("signal") or "", 0),
        claim.get("winning_score") or 0,
        claim.get("confidence", 0.0),
    )


def _to_hypothesis(claim: dict[str, Any], index: int) -> Hypothesis:
    statement = claim["canonical_statement"]
    segment, segment_trace = infer_segment(statement)
    signal = claim.get("signal") or "unsignalled"
    score = claim.get("winning_score") or 0
    return Hypothesis(
        id=f"HYP-{index:03d}",
        statement=f"Counter-position against: {statement}",
        counters_canonical_id=claim["id"],
        segment=segment,
        source_winning_score=score,
        rationale=(
            f"{claim['id']} is a {signal} at {score}/100 "
            f"(confidence {claim.get('confidence', 0.0)}); segment {segment_trace}"
        ),
    )

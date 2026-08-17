"""Enriched ledger digest — what the orchestrator sees at synthesis time.

Split out of agent/loop.py to keep that file under the project's 400-line cap
(CLAUDE.md §14). Pure formatting, no I/O: every field here already lives on the
Claim/CanonicalClaim models, computed deterministically upstream (ledger/signal.py,
ledger/confidence.py, ledger/clustering.py).
"""

from __future__ import annotations

from collections import Counter

from ledger.confidence import estimative_label
from ledger.models import CanonicalClaim, Claim
from ledger.signal import AD_CATEGORIES, longevity_label


def _ad_days_running(claim: Claim) -> int | None:
    """Max observed days-running across an ad claim's dated evidence; None if undated."""
    days = [
        (e.last_seen - e.first_seen).days
        for e in claim.evidence
        if e.first_seen is not None and e.last_seen is not None
    ]
    return max(days) if days else None


def claim_digest_line(claim: Claim) -> str:
    extras = [claim.observed_vs_inferred, estimative_label(claim.confidence)]
    if claim.signal:
        extras.append(f"signal={claim.signal} ({claim.signal_trace})")
    if claim.category in AD_CATEGORIES:
        days = _ad_days_running(claim)
        if days is not None:
            extras.append(f"longevity: {days}d — {longevity_label(days)}")
    if claim.canonical_id:
        extras.append(f"canonical={claim.canonical_id}")
    if claim.source_tool:
        extras.append(f"via {claim.source_tool}")
    return (
        f"[{claim.id}] ({claim.category}, conf {claim.confidence}, {', '.join(extras)}) "
        f"{claim.statement}"
    )


def canonical_digest_line(canonical: CanonicalClaim) -> str:
    extras = [
        f"sources={canonical.independent_source_count}",
        estimative_label(canonical.confidence),
    ]
    if canonical.winning_score is not None:
        extras.append(canonical.winning_score_trace)
    if canonical.signal:
        extras.append(f"signal={canonical.signal} ({canonical.signal_trace})")
    return (
        f"[{canonical.id}] ({canonical.category}, conf {canonical.confidence}, "
        f"{', '.join(extras)}) {canonical.canonical_statement}"
    )


def ledger_digest(ledger: list[Claim], canonical_claims: list[CanonicalClaim] | None = None) -> str:
    """The enriched digest sent to the model at synthesis time (Phase 6, Step 4).

    Two blocks: flat claims (every field the model needs to translate into the
    'What's Winning' / 'What Looks Like a Test' sections), then corroborated
    canonical claims — the [CAN-xxx] citation targets.
    """
    digest = "\n".join(claim_digest_line(c) for c in ledger)
    if canonical_claims:
        canon_block = "\n".join(canonical_digest_line(c) for c in canonical_claims)
        digest = f"{digest}\n\n## Corroborated (cross-source) claims\n{canon_block}"
    return digest


def source_coverage_line(ledger: list[Claim]) -> str:
    """Deterministic per-tool claim counts, prepended to the synthesis message.

    The synthesis call is one-shot and sees only the digest — without this the model
    cannot know which tools produced evidence and may claim a source returned nothing
    when it didn't (the 2026-07-28 wayback contradiction). Computed in Python, never
    model recall. Empty string when no claim carries a source_tool.
    """
    counts = Counter(c.source_tool for c in ledger if c.source_tool)
    if not counts:
        return ""
    parts = " · ".join(
        f"{tool}: {n} claim{'s' if n != 1 else ''}" for tool, n in sorted(counts.items())
    )
    return (
        f"Source coverage (deterministic, from the ledger): {parts}. "
        "Only describe a source as having returned nothing if it is absent from this line."
    )


def digest_preview(digest: str, width: int = 80) -> str:
    """One-line console preview of a tool digest: first line, ellipsis on truncation.

    The progress stream used to print a raw ``digest[:80]`` slice — mid-word cuts and,
    when the slice crossed a newline, stray fragments like ``[CLM-0`` on their own line.
    """
    first = digest.splitlines()[0] if digest else ""
    if len(first) <= width:
        return first
    return first[: width - 1] + "…"

"""Session state — the live conversation thread the REPL resumes across turns.

Approach A (spec: docs/superpowers/specs/2026-07-14-interactive-repl-design.md):
follow-ups append to the SAME message thread, so the orchestrator already holds the
claim digests in (prompt-cached) context and can decide 'answer from the ledger' vs
'call more tools' without re-crawling — DRY(E) made visible.

One Session per competitor. 'analyze <other-competitor>' starts a fresh Session;
the step budget is per-session, shared by the initial run and every follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.cost import CostAccumulator
from ledger.models import CanonicalClaim, Claim


@dataclass
class Session:
    """Mutable per-competitor run state threaded through the loop and follow-ups.

    claim_counter is a 1-element list so _dispatch can increment it in place
    (CLM ids stay unique across the initial run and later follow-ups).
    """

    competitor: str
    messages: list[dict] = field(default_factory=list)
    ledger: list[Claim] = field(default_factory=list)
    claim_counter: list[int] = field(default_factory=lambda: [0])
    cost: CostAccumulator = field(default_factory=CostAccumulator)
    canonical_claims: list[CanonicalClaim] = field(default_factory=list)
    steps: int = 0

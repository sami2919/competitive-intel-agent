"""Pydantic models for the CRO agent. Frozen — same immutability rule as ledger/models.py.

Two fields exist purely to make the deterministic gates possible, and they are the
reason this is not a prompt wrapper:

  Variant.claim_refs      -> compliance.py maps every comparative assertion back to a
                             CLM/CAN id in the shipped ledger, or an allowlisted
                             first-party fact. Unmappable comparative claim = rejected.
  Variant.changed_elements -> scoring.py enforces one independent variable per variant.
                             A variant that changes headline AND cta AND offer is not a
                             test, it's a redesign, and its result is unreadable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Variants scoring below this never reach the ship list — they go to a "Rejected
# variants" appendix with the reason. Mirrors ledger CONFIDENCE_GATE (0.5): it is
# better to ship three variants than six, if three make claims we cannot source.
CRO_SCORE_GATE = 60

ElementRole = Literal["hero", "subhead", "cta", "offer", "proof"]

# Why a variant was rejected. Gate failures are categorical, not score penalties.
RejectReason = Literal[
    "unsourced_claim",  # comparative assertion with no ledger/first-party backing
    "multivariate",  # changed more than one independent element
    "below_gate",  # passed both gates but scored under CRO_SCORE_GATE
    "segment_mismatch",  # declared a segment the source claim does not support
]


class PageElement(BaseModel):
    """One addressable copy block on the live page."""

    model_config = {"frozen": True}

    role: ElementRole
    text: str
    selector_hint: str = ""  # best-effort CSS/text anchor for the experiment tool


class PageSnapshot(BaseModel):
    """The live page as fetched. The control arm of every test."""

    model_config = {"frozen": True}

    url: str
    fetched_at: datetime
    elements: list[PageElement] = Field(min_length=1)
    raw_excerpt: str = ""

    def element(self, role: ElementRole) -> PageElement | None:
        return next((e for e in self.elements if e.role == role), None)


class Hypothesis(BaseModel):
    """A testable belief, derived deterministically from the competitor ledger.

    Never invented by the model — hypotheses are selected and ranked in
    cro/hypotheses.py from CanonicalClaims that already cleared the ledger's
    confidence gate. That provenance is what makes them defensible in a readout.
    """

    model_config = {"frozen": True}

    id: str  # HYP-001
    statement: str
    counters_canonical_id: str  # CAN-008 — the competitor claim this attacks
    segment: str  # e.g. "30-200 employee migration"
    source_winning_score: int  # carried from CanonicalClaim.winning_score
    rationale: str  # why this is worth a test slot


class Variant(BaseModel):
    """One generated test arm. Unscored until it passes through cro/scoring.py."""

    model_config = {"frozen": True}

    id: str  # VAR-001
    hypothesis_id: str
    headline: str
    subhead: str = ""
    cta: str = ""
    segment: str
    changed_elements: list[ElementRole] = Field(min_length=1)
    claim_refs: list[str] = Field(default_factory=list)  # CLM-xxx / CAN-xxx / FACT-xxx
    generated_by: str  # model + prompt version, e.g. "sonnet-5/variant_gen_v1"


class ScoredVariant(BaseModel):
    """A variant after the deterministic rubric. `trace` is the audit record."""

    model_config = {"frozen": True}

    variant: Variant
    score: int
    trace: str
    shippable: bool
    reject_reason: RejectReason | None = None

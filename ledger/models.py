"""Pydantic models for the claims ledger.

All models are frozen (immutable) — ledger updates return new objects, never mutate
claims in place (coding standards: immutable patterns).

Eng-review lock-in:
  D2 — CanonicalClaim groups same-statement claims across sources for the 0.9 corroboration tier.
  D6 — Evidence.utm_params carries zero-token UTM extraction (parsed with urllib.parse, no LLM).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# A claim must carry at least one piece of evidence — enforced at the model level.
MIN_EVIDENCE = 1
# Claims below this confidence never reach the brief body — they go to an
# "Unverified signals" appendix. "It's better to skip a prospect than send garbage."
CONFIDENCE_GATE = 0.5

ClaimCategory = Literal[
    "messaging",
    "positioning",
    "pricing",
    "ads_paid_social",
    "ads_search",
    "recent_change",
    "icp_targeting",
    "social_content",
]


class SourceResult(BaseModel):
    """Raw output of a source tool before extraction."""

    model_config = {"frozen": True}

    source: str  # tool name, e.g. "meta_ads"
    url: str
    fetched_at: datetime
    raw_excerpt: str
    status: Literal["ok", "empty", "partial"] = "ok"


class ToolFailure(BaseModel):
    """A typed tool failure — DATA the orchestrator routes around, never an exception."""

    model_config = {"frozen": True}

    tool: str
    reason: str
    suggestion: str = ""
    retriable: bool = False


class Evidence(BaseModel):
    """A single source backing a claim (>= 1 enforced per claim)."""

    model_config = {"frozen": True}

    source_url: str
    excerpt: str
    fetched_at: datetime
    # D6 — zero-token UTM extraction. None when the URL carries no utm_* params.
    utm_params: dict[str, str] | None = None
    # Ad longevity inputs (Phase 6): copied verbatim from ad metadata already present
    # in the raw excerpt (start_date / first_shown / last_shown). None when the source
    # isn't dated (e.g. crawl_site, press) or the extractor found no date text.
    first_seen: datetime | None = None
    last_seen: datetime | None = None


# Signal tiers — deterministic classification (ledger/signal.py), never model vibes.
# durable_pillar / cross_channel_winner: cross-source corroboration (clustering only).
# likely_winner / possible_test: single-source ad-longevity inference (build.py only).
SignalTier = Literal["durable_pillar", "cross_channel_winner", "likely_winner", "possible_test"]


class Claim(BaseModel):
    """One extracted, evidenced assertion in the ledger."""

    model_config = {"frozen": True}

    id: str  # CLM-001, CLM-002, ...
    competitor: str
    category: ClaimCategory
    statement: str
    evidence: list[Evidence] = Field(min_length=MIN_EVIDENCE)
    confidence: float
    confidence_trace: str  # WHY this score — the auditability Will wants
    extracted_by: str  # model + prompt version, e.g. "haiku-4-5/extractor_v1"
    # Observed (directly stated) vs inferred (reasoned) — always set by claim_from_raw;
    # first-class replacement for the old category-only inferred check.
    observed_vs_inferred: Literal["observed", "inferred"]
    # Deterministic "what's winning / what's a test" signal (Phase 6). None when no
    # classifier applies (most claims — pricing, positioning, single-mention messaging).
    signal: SignalTier | None = None
    signal_trace: str = ""
    # Backlink set after clustering: which CanonicalClaim (if any) this claim was
    # merged into. None for claims that stayed singleton.
    canonical_id: str | None = None
    # Source tool name (e.g. "crawl_site", "meta_ads") — set by claim_from_raw, which
    # already receives it. Default "" keeps every pre-existing direct Claim(...) test
    # construction valid; ledger/summarize.py needs it to group by source tool.
    source_tool: str = ""

    @model_validator(mode="after")
    def _validate_confidence(self) -> Claim:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        return self


class CanonicalClaim(BaseModel):
    """A cluster of same-statement claims from independent sources (eng review D2).

    The 0.9 confidence tier requires '2+ independent sources agree.' Per-source
    extraction produces separate Claim objects, so the same angle found on a
    competitor's homepage AND in their Meta ads is two claims, not one corroborated
    claim. Haiku-assisted clustering groups them here; confidence is then computed
    from the cluster's independent source count.
    """

    model_config = {"frozen": True}

    id: str  # CAN-001, ...
    competitor: str
    category: ClaimCategory
    canonical_statement: str
    member_claim_ids: list[str] = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=MIN_EVIDENCE)
    confidence: float
    confidence_trace: str
    signal: SignalTier | None = None
    signal_trace: str = ""
    # Deterministic 0-100 winning score (corroboration + persistence + recency).
    # None until clustering sets it. Optional so canonical JSON produced before the
    # trustworthiness pass still loads.
    winning_score: int | None = None
    winning_score_trace: str = ""

    @property
    def independent_source_count(self) -> int:
        # Distinct source URLs = independent sources. Same URL twice does not corroborate.
        return len({e.source_url for e in self.evidence})

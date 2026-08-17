"""Grounding validator — every brief sentence maps to a claim ID (arXiv 2604.03173).

The synthesis prompt may only cite claim IDs (e.g. [CLM-001]). A post-generation check
maps every factual sentence in the brief to a claim ID. Sub-gate claims (confidence <
CONFIDENCE_GATE) may only appear in a permitted hypothesis zone — the "Unverified signals"
appendix OR the "What Looks Like a Test" section — never the rest of the body.

"What Looks Like a Test" is a permitted zone because it is *defined* as `possible_test`
claims, which are ads < 90 days old → confidence 0.3 (ad base, no longevity boost) →
sub-gate by construction. Quarantining them out of that section would delete the section's
entire purpose. The prompt requires every bullet there to be framed as a hypothesis
("may be testing..."), so a sub-gate citation inside it is an explicit hypothesis, not a
strategic signal stated as fact. Sub-gate claims anywhere else (e.g. "What's Winning",
"What Changed Recently") still fail grounding.

Reference: arXiv 2604.03173 (OpenAI Deep Research: 3.5% hallucinated + 10.1% non-resolving
URLs) — we measure both via this grounding check and the URL health eval. A brief that fails
grounding is a hallucinated-precursor risk and must not be shipped.

Post-generation passes -> brief is grounded, gated, cited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ledger.models import CONFIDENCE_GATE, CanonicalClaim, Claim

# Matches [CLM-001] and [CAN-001] style citations — CanonicalClaim uses its own ID
# scheme for cross-source-corroborated claims (Phase 6).
_CLM_PATTERN = re.compile(r"\[(CLM-\d+|CAN-\d+)\]")


@dataclass(frozen=True)
class GroundingFinding:
    """A finding about a citation issue in the brief.

    Attributes:
        citation: The claim ID cited, e.g. "CLM-042".
        issue: Human-readable description of what is wrong.
        context: Surrounding text from the brief (up to ~160 chars) to help
                 locate the issue during review.
    """

    citation: str
    issue: str
    context: str


@dataclass(frozen=True)
class GroundingReport:
    """Result of grounding a brief against the claims ledger.

    Attributes:
        cited_ids: All [CLM-xxx] IDs found in the brief, in sorted order.
        missing_from_ledger: IDs cited in the brief that have no matching Claim.
        subgate_in_body: Findings for sub-gate claims cited outside the appendix.
    """

    cited_ids: list[str]
    missing_from_ledger: list[str]
    subgate_in_body: list[GroundingFinding]
    supragate_in_appendix: list[GroundingFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True iff no missing citations, no sub-gate claims in the body, and no
        body-eligible claims hidden in the Unverified appendix.

        A brief must pass before the grounding step is considered complete.
        The synthesis layer regenerates (max 2 retries), then flags for human
        review if still failing.
        """
        return (
            not self.missing_from_ledger
            and not self.subgate_in_body
            and not self.supragate_in_appendix
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Headings that mark a permitted zone for sub-gate (confidence < gate) citations.
# "Unverified signals" = the raw appendix dump. "What Looks Like a Test" = the
# interpreted hypothesis section (its claims are possible_test ads, sub-gate by
# construction). "Campaign test hypotheses" = ranked ad-test bets, hypothesis-framed
# by construction. Sub-gate claims anywhere else fail grounding.
_APPENDIX_SUBHEADINGS = ("unverified",)
_HYPOTHESIS_SUBHEADINGS = ("what looks like a test", "campaign test hypotheses")
_PERMITTED_SUBHEADINGS = _APPENDIX_SUBHEADINGS + _HYPOTHESIS_SUBHEADINGS


def _find_zone_ranges(brief: str, subheadings: tuple[str, ...]) -> list[tuple[int, int]]:
    """Find the character ranges [start, end) of every zone named by ``subheadings``.

    A zone starts at any heading (any level) whose text contains one of the given
    subheadings (case-insensitive) and extends to the next ``##`` heading or end of
    string. Returns ranges in document order; may be empty.
    """
    heading_re = re.compile(
        r"^#{1,6}\s+.*(?:" + "|".join(subheadings) + r")",
        re.MULTILINE | re.IGNORECASE,
    )
    next_h2 = re.compile(r"^##\s", re.MULTILINE)

    ranges: list[tuple[int, int]] = []
    for m in heading_re.finditer(brief):
        rest_of_brief = brief[m.end() :]
        next_m = next_h2.search(rest_of_brief)
        end = m.end() + next_m.start() if next_m else len(brief)
        ranges.append((m.start(), end))
    return ranges


def _extract_context(text: str, match: re.Match, window: int = 80) -> str:
    """Return a truncated window of text around a citation match for context."""
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    ctx = text[start:end].replace("\n", " ")
    if start > 0:
        ctx = "..." + ctx
    if end < len(text):
        ctx = ctx + "..."
    return ctx


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_grounding(
    brief: str,
    claims: list[Claim],
    canonical_claims: list[CanonicalClaim] | None = None,
    *,
    chat_mode: bool = False,
) -> GroundingReport:
    """Validate that every [CLM-xxx]/[CAN-xxx] citation in the brief maps to a known claim.

    Steps:
      1. Extract all [CLM-xxx]/[CAN-xxx] citations from the brief via regex.
      2. Check that every cited ID exists in the provided claims/canonical_claims.
      3. For sub-gate claims (confidence < CONFIDENCE_GATE), verify they appear
         ONLY inside the "Unverified signals" appendix section — never in the body.
         (CanonicalClaims are always >= the max of their members' confidence in
         practice, but the check applies symmetrically rather than assuming.)

    Args:
        brief: The markdown brief text to validate.
        claims: The full claims ledger for this competitor.
        canonical_claims: Cross-source-corroborated claims (CAN-xxx IDs), if any.
        chat_mode: When True (follow-up / compare answers), there are no section
            zones, so sub-gate claims are allowed anywhere (the prompt requires in-line
            "unverified"/"test" labeling); only missing IDs fail. Default False (brief).

    Returns:
        A GroundingReport summarising any issues found.
    """
    known: dict[str, float] = {c.id: c.confidence for c in claims}
    known.update({c.id: c.confidence for c in canonical_claims or []})

    matches = list(_CLM_PATTERN.finditer(brief))
    cited_ids = sorted({m.group(1) for m in matches})

    # Chat mode: no section structure — sub-gate claims are labeled in-line via the
    # follow-up/compare prompt, so the section-zone checks don't apply.
    permitted_ranges = _find_zone_ranges(brief, _PERMITTED_SUBHEADINGS) if not chat_mode else []
    appendix_ranges = _find_zone_ranges(brief, _APPENDIX_SUBHEADINGS) if not chat_mode else []

    missing: list[str] = []
    subgate_findings: list[GroundingFinding] = []
    supragate_findings: list[GroundingFinding] = []
    missing_seen: set[str] = set()

    for m in matches:
        cid = m.group(1)
        pos = m.start()

        if cid not in known:
            if cid not in missing_seen:
                missing.append(cid)
                missing_seen.add(cid)
            continue

        # Chat mode: only missing IDs fail (in-line labeling via the prompt).
        if chat_mode:
            continue

        confidence = known[cid]
        if confidence >= CONFIDENCE_GATE:
            # Symmetric gate check (failure_log F14): a body-eligible claim listed
            # under "Unverified signals" contradicts the appendix's own "confidence
            # < gate" header — the deterministic-gate story falsified in-brief.
            if any(start <= pos < end for start, end in appendix_ranges):
                supragate_findings.append(
                    GroundingFinding(
                        citation=cid,
                        issue=f"Body-eligible claim (confidence={confidence} >= "
                        f"{CONFIDENCE_GATE}) listed in the 'Unverified signals' "
                        f"appendix — it belongs in the body",
                        context=_extract_context(brief, m),
                    )
                )
            continue

        # Sub-gate — permitted inside any hypothesis zone (Test section or appendix).
        if any(start <= pos < end for start, end in permitted_ranges):
            continue

        subgate_findings.append(
            GroundingFinding(
                citation=cid,
                issue=f"Sub-gate claim (confidence={confidence}) cited in body, "
                f"not in a permitted hypothesis zone ('What Looks Like a Test' "
                f"or 'Unverified signals')",
                context=_extract_context(brief, m),
            )
        )

    return GroundingReport(
        cited_ids=cited_ids,
        missing_from_ledger=missing,
        subgate_in_body=subgate_findings,
        supragate_in_appendix=supragate_findings,
    )

"""Compliance gate — every assertion in generated copy must trace to a usable source.

Competitor claims are evidenced by the ledger (CLM-xxx / CAN-xxx). Claims about
OURSELVES need their own evidence, and that is data/rippling_facts.yaml (FACT-xxx).
Anything a variant asserts that maps to neither gets the variant rejected outright by
cro/scoring.py — comparative advertising claims carry real legal exposure, and this is
the one check a growth org cannot skip.

A fact with verified: false is treated EXACTLY like an unsourced claim. An allowlist
that trusts its own unverified entries is not an allowlist.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from cro.models import Variant
from cro.scoring import _content_stems


class Fact(BaseModel):
    """One allowlisted first-party assertion about Rippling."""

    model_config = {"frozen": True}

    id: str
    statement: str
    category: str = ""
    source_url: str = ""
    verified: bool = False


def load_facts(path: str | Path) -> dict[str, Fact]:
    """Read the allowlist, returning ONLY usable facts keyed by id.

    Usable = verified AND carrying a source_url. Raises on a malformed file:
    this is startup config, so fail fast rather than silently allowing nothing.
    """
    data = yaml.safe_load(Path(path).read_text()) or {}
    raw = data.get("facts")
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a top-level 'facts' list")
    facts = [Fact(**entry) for entry in raw]
    return {f.id: f for f in facts if f.verified and f.source_url}


# Copy that asserts something checkable and therefore requires a source.
_COMPARATIVE_TRIGGERS = (
    "than",
    " vs ",
    "versus",
    "unlike",
    "compared to",
    "best",
    "fastest",
    "cheapest",
    "only",
    "#1",
    "most",
    "leading",
)
_QUANTITATIVE = re.compile(r"\d")

# Competitors the ledger can evidence claims about. Lowercase for matching.
KNOWN_COMPETITORS = frozenset(
    {"gusto", "deel", "bamboohr", "justworks", "paylocity", "adp", "remote", "trinet"}
)

AssertionKind = Literal["comparative", "competitor", "quantitative"]


class Assertion(BaseModel):
    """A span of copy that requires a source. `trigger` says what flagged it."""

    model_config = {"frozen": True}

    kind: AssertionKind
    text: str
    trigger: str


def detect_assertions(
    text: str, competitors: frozenset[str] = KNOWN_COMPETITORS
) -> list[Assertion]:
    """Find copy that asserts something checkable. Deterministic, no LLM.

    Each distinct trigger is reported once so the unsourced count is not inflated
    by a phrase repeating.
    """
    low = text.lower()
    found: list[Assertion] = []

    comparative = next((t for t in _COMPARATIVE_TRIGGERS if t in low), None)
    if comparative:
        found.append(Assertion(kind="comparative", text=text, trigger=comparative.strip()))

    for name in sorted(competitors):
        if name in low:
            found.append(Assertion(kind="competitor", text=text, trigger=name))

    if _QUANTITATIVE.search(text):
        found.append(Assertion(kind="quantitative", text=text, trigger="numeral"))

    return found


class ComplianceReport(BaseModel):
    """Whether a variant's assertions trace to usable sources. `unsourced` feeds scoring."""

    model_config = {"frozen": True}

    variant_id: str
    assertions: list[Assertion] = []
    unsourced: list[str] = []
    invalid_refs: list[str] = []
    compliant: bool = True
    trace: str = ""


def check_compliance(
    variant: Variant, ledger_ids: dict[str, str], facts: dict[str, Fact]
) -> ComplianceReport:
    """Map every assertion in the variant's copy to a usable ref. Never raises.

    COVERAGE IS A HEURISTIC: we cannot deterministically prove which ref backs
    which assertion, so an assertion counts as covered when it shares at least one
    crude-stemmed content word with the statement of any usable ref. That correctly
    rejects the failure that matters — copy asserting something no cited source is
    even about — while not pretending to be a proof.
    """
    copy = f"{variant.headline} {variant.subhead}".strip()
    assertions = detect_assertions(copy)

    usable: dict[str, str] = {}
    invalid: list[str] = []
    for ref in variant.claim_refs:
        if ref in ledger_ids:
            usable[ref] = ledger_ids[ref]
        elif ref in facts:
            usable[ref] = facts[ref].statement
        else:
            invalid.append(ref)

    unsourced: list[str] = []
    if assertions:
        supported: set[str] = set()
        for statement in usable.values():
            supported |= _content_stems(statement)
        if not _content_stems(copy) & supported:
            unsourced = sorted({a.text for a in assertions})

    compliant = not unsourced and not invalid
    trace = (
        f"{len(assertions)} assertion(s) requiring backing · "
        f"{len(usable)} usable ref(s) · {len(invalid)} invalid · "
        f"coverage by stemmed content-word overlap (heuristic) → "
        f"{'compliant' if compliant else 'NOT compliant'}"
    )
    return ComplianceReport(
        variant_id=variant.id,
        assertions=assertions,
        unsourced=unsourced,
        invalid_refs=invalid,
        compliant=compliant,
        trace=trace,
    )

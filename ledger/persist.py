"""Ledger persistence and re-run diffing (CLAUDE.md §5).

Turns the agent from a one-shot into a weekly competitor watch. The claims ledger
persists to disk per competitor (outputs/{slug}_intel.json); subsequent re-runs diff
against the prior ledger so analysts see what's new, what changed, and what disappeared.

API summary:
    save_ledger(claims, slug)   -> Path  (writes canonical JSON)
    load_ledger(slug)           -> list[Claim] | None
    diff_ledgers(old, new)      -> LedgerDiff
    ledger_path(slug)           -> Path

Diff matching is by *normalized statement* (not claim ID) so that IDs changing across
runs doesn't produce false-positive removals.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ledger.models import CanonicalClaim, Claim


@dataclass(frozen=True)
class LedgerDiff:
    """The delta between two runs for a competitor.

    Attributes:
        new_claim_ids: IDs of claims present in the new ledger but not the old one.
        removed_claim_ids: IDs of claims present in the old ledger but not the new one.
        unchanged_count: Number of claims whose normalized statement appears in both.
        summary: Human-readable string like "N new, M removed, K unchanged".
    """

    new_claim_ids: tuple[str, ...]
    removed_claim_ids: tuple[str, ...]
    unchanged_count: int = field(compare=False)
    summary: str = field(compare=False)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def ledger_path(slug: str, outputs_dir: Path = Path("outputs")) -> Path:
    """Return the filesystem path for a competitor's serialised ledger.

    Example: outputs/gusto.com_intel.json
    """
    return outputs_dir / f"{slug}_intel.json"


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def save_ledger(claims: list[Claim], slug: str, outputs_dir: Path = Path("outputs")) -> Path:
    """Persist the claims ledger to disk as JSON.

    Uses model_dump(mode="json") for a serialisable dict that round-trips through
    Claim.model_validate. The file is written with indent=2 for human readability.

    Args:
        claims: The full claims ledger for this competitor.
        slug: Competitor identifier (e.g. "gusto.com").
        outputs_dir: Target directory (defaults to "outputs/").

    Returns:
        The Path that was written to.
    """
    outputs_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_path(slug, outputs_dir)

    serialisable: list[dict[str, Any]] = [c.model_dump(mode="json") for c in claims]
    path.write_text(
        json.dumps(serialisable, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_ledger(slug: str, outputs_dir: Path = Path("outputs")) -> list[Claim] | None:
    """Load a previously-persisted claims ledger from disk.

    Returns None when no prior file exists (first run for this competitor).
    Raises on malformed JSON so the caller can surface the error during
    investigation rather than silently producing a diffless run.

    Args:
        slug: Competitor identifier (e.g. "gusto.com").
        outputs_dir: Target directory (defaults to "outputs/").

    Returns:
        The deserialised list of Claims, or None if the file does not exist.
    """
    path = ledger_path(slug, outputs_dir)
    if not path.exists():
        return None

    raw: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return [Claim.model_validate(item) for item in raw]


def canonical_path(slug: str, outputs_dir: Path = Path("outputs")) -> Path:
    """Return the filesystem path for a competitor's canonical-claims JSON."""
    return outputs_dir / f"{slug}_canonical.json"


def load_canonical_claims(
    slug: str, outputs_dir: Path = Path("outputs")
) -> list[CanonicalClaim] | None:
    """Load a previously-persisted canonical-claims list (the [CAN-xxx] clusters).

    Returns None when no canonical file exists (a pre-Phase-6 run, or a competitor whose
    claims never clustered). Used by compare_with_rippling (Phase 7) to load Rippling's
    canonical claims alongside its flat ledger.
    """
    path = canonical_path(slug, outputs_dir)
    if not path.exists():
        return None
    raw: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return [CanonicalClaim.model_validate(item) for item in raw]


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _normalize_statement(text: str) -> str:
    """Normalize a claim statement for diff matching.

    Lowercases, strips leading/trailing whitespace, and collapses internal
    whitespace runs into a single space so that minor punctuation or spacing
    differences don't produce false-positive mismatches.
    """
    return re.sub(r"\s+", " ", text.strip().lower())


# Minimum token-set Jaccard overlap for two statements to count as the same claim
# reworded. Tuned against real re-run drift: Haiku phrases the same fact differently
# each run ("...than competitors" vs "...than rivals"), which exact matching read as
# 80 new / 119 removed on a day nothing changed (monitoring noise, failure_log F17).
_FUZZY_JACCARD_THRESHOLD = 0.6


def _token_set(statement: str) -> frozenset[str]:
    return frozenset(_normalize_statement(statement).split())


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def diff_ledgers(old: list[Claim], new: list[Claim]) -> LedgerDiff:
    """Compute the delta between two ledger versions.

    Matching is by *normalized statement* (not claim ID) so that IDs changing
    across runs doesn't produce false-positive removals — exact matches first,
    then a greedy fuzzy pass (token-set Jaccard >= 0.6) so extractor rewording
    of the same fact counts as "unchanged" rather than churn. A claim is
    "unchanged" when it matches either way, even if confidence, evidence, or
    extracted_by fields differ.

    Args:
        old: The prior run's claims ledger.
        new: The current run's claims ledger.

    Returns:
        A LedgerDiff describing what is new, what was removed, and what stayed.
    """
    old_normalized: dict[str, str] = {_normalize_statement(c.statement): c.id for c in old}
    new_normalized: dict[str, str] = {_normalize_statement(c.statement): c.id for c in new}

    exact_unchanged = set(old_normalized) & set(new_normalized)
    old_leftover = set(old_normalized) - exact_unchanged
    new_leftover = set(new_normalized) - exact_unchanged

    # Greedy fuzzy pass over the leftovers: best Jaccard first, each statement
    # matched at most once. O(n*m) on the leftovers only — ledgers are ~100 claims.
    scored = sorted(
        (
            (_jaccard(_token_set(o), _token_set(n)), o, n)
            for o in old_leftover
            for n in new_leftover
        ),
        key=lambda t: t[0],
        reverse=True,
    )
    fuzzy_matched = 0
    matched_old: set[str] = set()
    matched_new: set[str] = set()
    for score, o, n in scored:
        if score < _FUZZY_JACCARD_THRESHOLD:
            break
        if o in matched_old or n in matched_new:
            continue
        matched_old.add(o)
        matched_new.add(n)
        fuzzy_matched += 1

    new_ids = tuple(sorted(new_normalized[s] for s in new_leftover - matched_new))
    removed_ids = tuple(sorted(old_normalized[s] for s in old_leftover - matched_old))
    unchanged_count = len(exact_unchanged) + fuzzy_matched

    fuzzy_note = f" ({fuzzy_matched} reworded)" if fuzzy_matched else ""
    return LedgerDiff(
        new_claim_ids=new_ids,
        removed_claim_ids=removed_ids,
        unchanged_count=unchanged_count,
        summary=(
            f"{len(new_ids)} new, {len(removed_ids)} removed, "
            f"{unchanged_count} unchanged{fuzzy_note}"
        ),
    )

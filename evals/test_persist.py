"""Persist + re-run diff tests for ledger/persist.py.

Covers five acceptance scenarios:
  (a) save_ledger then load_ledger round-trips claims faithfully.
  (b) diff_ledgers: 2 old + 3 new (2 same, 1 new) => 1 new, 0 removed, 2 unchanged.
  (c) diff_ledgers: a claim removed between runs => removed_claim_ids populated.
  (d) load_ledger on nonexistent slug => None.
  (e) diff matches by statement not ID (same statement, different IDs => unchanged).

Every test runs offline with synthetic fixtures; no API keys or network required.
Uses tmp_path for filesystem isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ledger.models import Claim, Evidence
from ledger.persist import (
    LedgerDiff,
    diff_ledgers,
    ledger_path,
    load_ledger,
    save_ledger,
)


def _evidence(**overrides: object) -> Evidence:
    """Minimal valid Evidence factory."""
    base: dict = {
        "source_url": "https://gusto.com",
        "excerpt": "Test evidence",
        "fetched_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Evidence(**base)


def _claim(id: str, statement: str, confidence: float = 0.9, **overrides: object) -> Claim:
    """Minimal valid Claim factory. The statement argument is required so tests
    that depend on statement-matching are explicit about what they set."""
    base: dict = {
        "competitor": "gusto.com",
        "category": "messaging",
        "evidence": [_evidence()],
        "confidence_trace": "test 0.9",
        "extracted_by": "test/v1",
        "observed_vs_inferred": "observed",
    }
    base.update(overrides)
    return Claim(id=id, statement=statement, confidence=confidence, **base)


# ---------------------------------------------------------------------------
# (a) Round-trip
# ---------------------------------------------------------------------------


def test_save_then_load_roundtrip(tmp_path) -> None:
    """save_ledger + load_ledger round-trips claims faithfully."""
    claims = [
        _claim("CLM-001", "Gusto leads with simple pricing"),
        _claim("CLM-002", "Deel targets global companies"),
    ]
    slug = "gusto.com"

    saved_path = save_ledger(claims, slug, outputs_dir=tmp_path)
    assert saved_path == tmp_path / "gusto.com_intel.json"
    assert saved_path.exists()

    loaded = load_ledger(slug, outputs_dir=tmp_path)
    assert loaded is not None
    assert len(loaded) == 2

    for original, restored in zip(claims, loaded, strict=True):
        assert original.model_dump(mode="json") == restored.model_dump(mode="json")


# ---------------------------------------------------------------------------
# (b) New claims in diff
# ---------------------------------------------------------------------------


def test_diff_new_claims_detected() -> None:
    """Old has 2 claims, new has 3 (2 same + 1 new) => 1 new, 0 removed, 2 unchanged."""
    old = [
        _claim("CLM-001", "Gusto leads with simple pricing"),
        _claim("CLM-002", "Deel targets global companies"),
    ]
    new = [
        _claim("CLM-001", "Gusto leads with simple pricing"),
        _claim("CLM-002", "Deel targets global companies"),
        _claim("CLM-003", "Rippling offers IT + HR combined"),
    ]

    diff = diff_ledgers(old, new)
    assert diff.new_claim_ids == ("CLM-003",)
    assert diff.removed_claim_ids == ()
    assert diff.unchanged_count == 2
    assert "1 new" in diff.summary
    assert "0 removed" in diff.summary
    assert "2 unchanged" in diff.summary


# ---------------------------------------------------------------------------
# (c) Removed claims in diff
# ---------------------------------------------------------------------------


def test_diff_removed_claims_detected() -> None:
    """Old has 2 claims, new has 1 (the other removed)."""
    old = [
        _claim("CLM-001", "Gusto leads with simple pricing"),
        _claim("CLM-002", "Deel targets global companies"),
    ]
    new = [
        _claim("CLM-002", "Deel targets global companies"),
    ]

    diff = diff_ledgers(old, new)
    assert diff.new_claim_ids == ()
    assert diff.removed_claim_ids == ("CLM-001",)
    assert diff.unchanged_count == 1
    assert "0 new" in diff.summary
    assert "1 removed" in diff.summary


# ---------------------------------------------------------------------------
# (d) Nonexistent slug
# ---------------------------------------------------------------------------


def test_load_ledger_nonexistent_returns_none(tmp_path) -> None:
    """load_ledger on a slug with no prior file returns None."""
    loaded = load_ledger("nonexistent-competitor", outputs_dir=tmp_path)
    assert loaded is None


# ---------------------------------------------------------------------------
# (e) Diff matches by statement, not ID
# ---------------------------------------------------------------------------


def test_diff_matches_by_statement_not_id() -> None:
    """Same statement with different IDs across runs => unchanged (not removed+new)."""
    old = [
        _claim("CLM-001", "Gusto leads with simple pricing"),
    ]
    new = [
        _claim("CLM-999", "Gusto leads with simple pricing"),  # same statement, new ID
    ]

    diff = diff_ledgers(old, new)
    assert diff.new_claim_ids == ()
    assert diff.removed_claim_ids == ()
    assert diff.unchanged_count == 1
    assert "0 new" in diff.summary
    assert "0 removed" in diff.summary


# ---------------------------------------------------------------------------
# (f) Normalization handles whitespace and casing differences
# ---------------------------------------------------------------------------


def test_diff_normalizes_whitespace_and_case() -> None:
    """Extra whitespace and different casing should not break matching."""
    old = [
        _claim("CLM-001", "  Gusto Leads With  Simple Pricing  "),
    ]
    new = [
        _claim("CLM-001", "gusto leads with simple pricing"),
    ]

    diff = diff_ledgers(old, new)
    assert diff.unchanged_count == 1
    assert diff.new_claim_ids == ()
    assert diff.removed_claim_ids == ()


# ---------------------------------------------------------------------------
# (g) LedgerDiff is a frozen dataclass with the right fields
# ---------------------------------------------------------------------------


def test_ledger_diff_shape() -> None:
    """LedgerDiff exposes the expected attributes."""
    diff = LedgerDiff(
        new_claim_ids=("CLM-003",),
        removed_claim_ids=(),
        unchanged_count=2,
        summary="1 new, 0 removed, 2 unchanged",
    )
    assert diff.new_claim_ids == ("CLM-003",)
    assert diff.removed_claim_ids == ()
    assert diff.unchanged_count == 2
    assert diff.summary == "1 new, 0 removed, 2 unchanged"


# ---------------------------------------------------------------------------
# (h) ledger_path returns the expected path
# ---------------------------------------------------------------------------


def test_ledger_path_default() -> None:
    """ledger_path builds the correct default path."""
    path = ledger_path("gusto.com")
    assert path == Path("outputs") / "gusto.com_intel.json"


def test_ledger_path_custom_dir() -> None:
    """ledger_path accepts a custom outputs_dir."""
    path = ledger_path("gusto.com", outputs_dir=Path("/tmp/evals"))
    assert path == Path("/tmp/evals") / "gusto.com_intel.json"


def test_diff_fuzzy_matches_reworded_statement() -> None:
    """Haiku phrasing drift must not read as churn (live-run F: 80 new / 119 removed)."""
    old = [
        _claim("CLM-001", statement="Gusto positions itself as easier to use than competitors"),
        _claim("CLM-002", statement="Gusto offers unlimited payroll runs at no extra cost"),
    ]
    new = [
        _claim("CLM-101", statement="Gusto positions itself as easier to use than rivals"),
        _claim("CLM-102", statement="Something genuinely brand new about pricing tiers"),
    ]
    d = diff_ledgers(old, new)

    assert d.unchanged_count == 1  # reworded claim fuzzy-matches
    assert d.new_claim_ids == ("CLM-102",)
    assert d.removed_claim_ids == ("CLM-002",)


def test_diff_fuzzy_does_not_overmatch_different_statements() -> None:
    old = [_claim("CLM-001", statement="Gusto runs price-first SMB search ads")]
    new = [_claim("CLM-101", statement="Deel expands into field services for oil and gas")]
    d = diff_ledgers(old, new)

    assert d.unchanged_count == 0
    assert d.new_claim_ids == ("CLM-101",)
    assert d.removed_claim_ids == ("CLM-001",)

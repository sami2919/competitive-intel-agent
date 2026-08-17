"""Golden set — property-based regression checks (CLAUDE.md section 7).

These tests assert that a live run against a known competitor produces expected
marketing-intelligence signals. They are high-level regression checks that catch
regressions in the tool-execution, extraction, or synthesis pipeline for *real*
competitor data — not unit tests of ledger formatting.

Live runs (active properties):
  - Gusto — SMB/price-first positioning and ad presence.
  - Deel — global/EOR positioning (the EOR wedge).
Scaffolded (skip-gated):
  - BambooHR — must surface an HR-core signal.

NOTE: these tests require pre-computed output files. Run:
  make run COMPETITOR=gusto.com
  make run COMPETITOR=deel.com
  make run COMPETITOR=bamboohr.com

arXiv 2604.03173 (OpenAI Deep Research) reports 3.5% hallucinated claims and 10.1%
non-resolving URLs in LLM-generated research. Golden-set property checks complement
per-claim URL-health validation by catching systemic output degradation before brief
generation — if the Golden Gusto brief stops mentioning SMB signals or pricing claims,
something upstream broke, even if individual claims pass schema validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledger.models import Claim

OUTPUTS = Path(__file__).parents[2] / "outputs"


def _load_golden(slug: str) -> tuple[list[Claim], str | None]:
    """Load the ledger JSON + optional brief for a competitor slug.

    Returns (claims, brief_text).  Raises ``pytest.skip()`` if the ledger file
    does not exist — the caller is expected to run the competitor first.
    """
    ledger_path = OUTPUTS / f"{slug}_intel.json"
    brief_path = OUTPUTS / f"{slug}_brief.md"

    if not ledger_path.exists():
        pytest.skip(f"run `make run COMPETITOR={slug}` first — missing {ledger_path}")

    claims = [Claim.model_validate(c) for c in json.loads(ledger_path.read_text(encoding="utf-8"))]
    brief = brief_path.read_text(encoding="utf-8") if brief_path.exists() else None
    return claims, brief


# ── Gusto (live, active) ─────────────────────────────────────────────────


class TestGusto:
    """Property-based checks for the completed Gusto golden run."""

    def test_pricing_and_smb_signal(self) -> None:
        """Gusto MUST have pricing claims AND a brief mentioning 'small business' / '$49' / 'SMB'."""
        claims, brief = _load_golden("gusto.com")

        pricing_claims = [c for c in claims if c.category == "pricing"]
        assert len(pricing_claims) >= 1, f"Expected >=1 pricing claim, got {len(pricing_claims)}"

        assert brief is not None, "Brief file must exist to check SMB signal"
        lower = brief.lower()
        assert any(phrase in lower for phrase in ("$49", "small business", "smb")), (
            "Brief must mention $49, small business, or SMB"
        )

    def test_ad_presence(self) -> None:
        """Gusto MUST have at least one Meta or Google ad claim.

        Asserts on ``source_tool`` (provenance), not ``category``: the extractor assigns a
        *semantic* category (an ad saying "no hidden fees" is `pricing`), so an ad-sourced
        claim is not guaranteed to carry category `ads_paid_social`/`ads_search`. The
        docstring intent — "a Meta or Google ad claim" — is provenance, which is what
        ``source_tool`` records.
        """
        claims, _brief = _load_golden("gusto.com")
        ad_claims = [c for c in claims if c.source_tool in ("meta_ads", "google_ads")]
        assert len(ad_claims) >= 1, (
            f"Expected >=1 ad-sourced claim (source_tool meta_ads or google_ads), "
            f"got {len(ad_claims)}"
        )


# ── Deel (live, active) ──────────────────────────────────────────────────


class TestDeel:
    """Contract: Deel must surface global/EOR positioning (the EOR wedge)."""

    def test_global_eor_positioning(self) -> None:
        claims, _brief = _load_golden("deel.com")
        positioning_claims = [
            c
            for c in claims
            if c.category == "positioning"
            and any(kw in c.statement.lower() for kw in ("global", "eor"))
        ]
        assert len(positioning_claims) >= 1, (
            f"Expected >=1 global/EOR positioning claim, got {len(positioning_claims)}"
        )


# ── BambooHR ─────────────────────────────────────────────────────────────
# (was skip-gated until a live BambooHR run existed; the 2026-07-15 interactive
# run produced outputs/bamboohr.com_intel.json, so the contract now runs — and
# still self-skips via _load_golden if the ledger file is ever absent.)


class TestBamboo:
    """Contract: BambooHR must surface an HR-core signal."""

    def test_hr_core_signal(self) -> None:
        claims, _brief = _load_golden("bamboohr.com")
        hr_claims = [
            c
            for c in claims
            if c.category in ("messaging", "positioning")
            and any(kw in c.statement.lower() for kw in ("hr", "human resources"))
        ]
        assert len(hr_claims) >= 1, (
            f"Expected >=1 HR-core claim (messaging/positioning w/ 'hr' or 'human resources'), "
            f"got {len(hr_claims)}"
        )

"""Deterministic summarize-block tests (ledger/summarize.py) — no LLM.

The confidence-by-source, evidence-quality, and how-to-read blocks are pure Python
appended post-generation. Zero hallucination risk, so these tests just assert the
deterministic output shape and numbers for a known claim set.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ledger.models import Claim, Evidence
from ledger.summarize import (
    format_confidence_by_source,
    format_evidence_quality,
    format_how_to_read,
)


def _evidence(url: str) -> Evidence:
    return Evidence(source_url=url, excerpt="x", fetched_at=datetime.now(UTC))


def _claim(cid: str, source_tool: str, confidence: float) -> Claim:
    return Claim(
        id=cid,
        competitor="gusto.com",
        category="messaging",
        statement="x",
        evidence=[_evidence(f"https://{cid}.com")],
        confidence=confidence,
        confidence_trace="t",
        extracted_by="test/v1",
        observed_vs_inferred="observed",
        source_tool=source_tool,
    )


def test_format_confidence_by_source_groups_and_averages() -> None:
    claims = [
        _claim("CLM-001", "crawl_site", 0.7),
        _claim("CLM-002", "crawl_site", 0.9),
        _claim("CLM-003", "news_press", 0.5),
    ]
    out = format_confidence_by_source(claims)
    assert "| crawl_site | 2 | 0.80 | primary |" in out
    assert "| news_press | 1 | 0.50 | secondary |" in out


def test_format_confidence_by_source_skips_unsourced() -> None:
    claims = [_claim("CLM-001", "crawl_site", 0.7)]
    # add a claim with no source_tool
    claims.append(
        Claim(
            id="CLM-002",
            competitor="gusto.com",
            category="messaging",
            statement="x",
            evidence=[_evidence("https://x.com")],
            confidence=0.7,
            confidence_trace="t",
            extracted_by="test/v1",
            observed_vs_inferred="observed",
        )
    )
    out = format_confidence_by_source(claims)
    assert "crawl_site" in out
    assert "CLM" not in out  # no claim IDs leak into the table


def test_format_evidence_quality_splits_primary_secondary_and_gate() -> None:
    claims = [
        _claim("CLM-001", "crawl_site", 0.7),  # primary, body-eligible
        _claim("CLM-002", "meta_ads", 0.3),  # primary, hypothesis-only
        _claim("CLM-003", "news_press", 0.5),  # secondary, body-eligible
        _claim("CLM-004", "g2_reviews", 0.5),  # secondary, body-eligible
    ]
    out = format_evidence_quality(claims)
    assert "## Evidence quality" in out
    assert "| Primary " in out and "| Secondary " in out
    # 2 primary claims, 2 secondary; 3 body-eligible (>=0.5), 1 hypothesis-only
    assert "| Primary (owned site / ad library / social / wayback) |" in out
    assert "Body-eligible (confidence >= 0.5): 3 of 4." in out
    assert "Hypothesis-only (confidence < 0.5): 1." in out


def test_format_evidence_quality_handles_empty() -> None:
    out = format_evidence_quality([])
    assert "## Evidence quality" in out
    assert "0 of 0" in out


def test_format_how_to_read_states_rules_and_grounding_guarantee() -> None:
    out = format_how_to_read()
    assert "## How to read this brief" in out
    assert "What's Winning" in out and "winning=NN/100" in out
    assert "What Looks Like a Test" in out and "HYPOTHESES" in out
    assert "What Changed Recently" in out and "strategic shifts" in out
    assert "Unverified signals" in out
    assert "Rippling-relevance" in out
    assert "Grounding" in out and "deterministic Python" in out

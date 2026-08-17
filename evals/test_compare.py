"""Comparative mode tests — compare_with_rippling (Phase 7, Layer 3, decision D2=A).

Compares the current competitor vs Rippling, citing BOTH ledgers. Rippling's IDs are
RIP--prefixed in the combined digest so [CLM-001] can't collide. load_ledger /
load_canonical_claims are monkeypatched so no on-disk Rippling ledger is required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.loop import compare_with_rippling
from agent.session import Session
from evals.stub import StubClient, StubResponse, TextBlock
from evals.test_grounding import _canonical, _claim


def _session_with_ledger() -> Session:
    s = Session(competitor="gusto.com")
    s.ledger = [
        _claim("CLM-001", 0.7, category="pricing", statement="Gusto charges a flat $6/user rate")
    ]
    s.claim_counter = [1]
    return s


def _rippling_claims() -> list:
    return [
        _claim(
            "CLM-001",
            0.7,
            competitor="rippling.com",
            category="positioning",
            statement="Rippling is a compound HR+IT+Finance platform",
        )
    ]


def test_compare_with_rippling_cites_both_ledgers(monkeypatch):
    monkeypatch.setattr(
        "agent.loop.load_ledger",
        lambda slug, outputs_dir=Path("outputs"): _rippling_claims(),
    )
    monkeypatch.setattr(
        "agent.loop.load_canonical_claims", lambda slug, outputs_dir=Path("outputs"): []
    )
    session = _session_with_ledger()
    script = [
        StubResponse(
            content=[
                TextBlock(
                    "Gusto and Rippling both lead with simplicity. Gusto [CLM-001] pitches "
                    "flat pricing; Rippling [RIP-CLM-001] pitches a compound platform. They "
                    "differ: Gusto stays SMB, Rippling scales."
                )
            ]
        )
    ]
    client = StubClient(sonnet_script=script, haiku_text="[]")

    answer = asyncio.run(compare_with_rippling(session, client))

    assert "[CLM-001]" in answer  # competitor side cited
    assert "[RIP-CLM-001]" in answer  # Rippling side cited


def test_compare_no_rippling_ledger_prompts_to_build(monkeypatch):
    monkeypatch.setattr("agent.loop.load_ledger", lambda slug, outputs_dir=Path("outputs"): None)
    session = _session_with_ledger()
    client = StubClient(
        sonnet_script=[StubResponse(content=[TextBlock("should not be reached")])],
        haiku_text="[]",
    )

    answer = asyncio.run(compare_with_rippling(session, client))

    assert "make run" in answer.lower()
    assert "rippling.com" in answer
    assert client.messages.calls == []  # no model call made


def test_compare_cites_rippling_canonical_claims(monkeypatch):
    """[RIP-CAN-xxx] citations resolve against the relabeled Rippling canonical claims
    in the combined grounding set (not just [RIP-CLM-xxx])."""
    monkeypatch.setattr(
        "agent.loop.load_ledger",
        lambda slug, outputs_dir=Path("outputs"): [
            _claim(
                "CLM-001",
                0.7,
                competitor="rippling.com",
                category="positioning",
                statement="Rippling is a compound platform",
            )
        ],
    )
    monkeypatch.setattr(
        "agent.loop.load_canonical_claims",
        lambda slug, outputs_dir=Path("outputs"): [
            _canonical(
                "CAN-001",
                0.9,
                competitor="rippling.com",
                canonical_statement="Rippling is a compound HR+IT+Finance platform",
                member_claim_ids=["CLM-001"],
            )
        ],
    )
    session = _session_with_ledger()
    script = [
        StubResponse(
            content=[
                TextBlock(
                    "Rippling's compound platform [RIP-CAN-001] contrasts with Gusto's "
                    "point solution [CLM-001]."
                )
            ]
        )
    ]
    client = StubClient(sonnet_script=script, haiku_text="[]")

    answer = asyncio.run(compare_with_rippling(session, client))

    assert "[RIP-CAN-001]" in answer
    assert "[CLM-001]" in answer

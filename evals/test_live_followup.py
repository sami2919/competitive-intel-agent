"""Live acceptance gate (Phase 7, Layer 4) — a REAL follow-up on the persisted Gusto ledger.

Green stubs are not sufficient proof (lessons.md meta-lesson: the regen bug was invisible
to the stubbed suite because stubs model call-sequence, not behavior). This gate calls the
real Anthropic API on a follow-up and asserts the answer is a scoped conversational reply,
NOT a regenerated 6-section brief.

Opt-in: skipped unless INTEL_LIVE=1 (run via `make eval-live`). NOT part of `make eval`,
which stays offline and free. ~$0.01/run (two Sonnet calls, tools=[] so no tool execution).
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("INTEL_LIVE"),
    reason="live gate — run `make eval-live` (calls the real Anthropic API, ~$0.01)",
)


def test_live_followup_does_not_regen_full_brief():
    from agent.config import load_env
    from agent.llm import make_client
    from agent.loop import follow_up
    from agent.session import Session
    from ledger.persist import load_canonical_claims, load_ledger

    load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    assert api_key, "ANTHROPIC_API_KEY required for the live gate (set in .env)"

    ledger = load_ledger("gusto.com")
    assert ledger, "no persisted Gusto ledger — run `make run COMPETITOR=gusto.com` first"

    session = Session(competitor="gusto.com")
    session.ledger = ledger
    session.canonical_claims = load_canonical_claims("gusto.com") or []
    session.claim_counter = [len(ledger)]

    client = make_client(api_key)
    answer = asyncio.run(follow_up(session, "dig deeper on the pricing", client, tools=[]))

    print("\n--- LIVE FOLLOW-UP ANSWER ---\n", answer, "\n--- END ---\n")

    # The regression guard: a follow-up must NOT come back as the full structured brief.
    assert "## What's Winning" not in answer
    assert "## What Changed Recently" not in answer
    assert "## Rippling-relevance" not in answer
    assert len(answer) > 50  # a real conversational answer, not a blank/error

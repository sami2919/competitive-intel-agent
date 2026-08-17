"""Session + follow-up tests — the conversational layer (Phase 5, approach A).

Asserts the spec's core properties:
  - the clarifying question pauses for a REAL answer via ask_user (called exactly once)
  - follow-ups append to the SAME message thread (live session, not a fresh conversation)
  - follow-up tool calls grow the same ledger with continuing CLM ids
  - step budget is per-session (shared by the initial run and follow-ups)
  - a new competitor gets a fresh Session (fresh thread, fresh ledger)
"""

from __future__ import annotations

import asyncio
import json

from agent.loop import follow_up, run_competitor
from evals.stub import StubClient, StubResponse, TextBlock, ToolUseBlock
from evals.test_loop import HAIKU_CLAIMS, _fixtures
from tools._transport import ReplayTransport
from tools.crawl_site import CrawlSiteTool
from tools.meta_ads import MetaAdsTool

PRICING_CLAIM = json.dumps(
    [
        {
            "statement": "Gusto charges $6 per user per month on the base plan",
            "category": "pricing",
            "source_url": "https://gusto.com/pricing",
            "excerpt": "Pricing from $39/mo + $6/user.",
        }
    ]
)


def _initial_script() -> list[StubResponse]:
    return [
        # 1. intake: ONE clarifying question
        StubResponse(content=[TextBlock("Should I focus SMB, enterprise, or both?")]),
        # 2. after the interactive answer: call crawl_site
        StubResponse(
            content=[
                ToolUseBlock(
                    id="t1", name="crawl_site", input={"domain": "gusto.com", "max_pages": 3}
                )
            ]
        ),
        # 3. spontaneous end-of-cycle text (no tool_use) -> ends the tool_use loop.
        # This is NOT the brief anymore -- the loop always makes a dedicated
        # explicit synthesis call afterward (agent/loop.py run_competitor).
        StubResponse(content=[TextBlock("Research looks sufficient.")]),
        # 4. explicit "write the brief" synthesis call ([CLM-002] is sub-gate -> appendix)
        StubResponse(
            content=[
                TextBlock(
                    "## Brief\nFlat-rate pricing [CLM-001].\n\n"
                    "## Unverified signals\n- SMB targeting [CLM-002]."
                )
            ]
        ),
        # 5. follow-up turn: dig deeper -> another crawl_site call
        StubResponse(
            content=[
                ToolUseBlock(
                    id="t2", name="crawl_site", input={"domain": "gusto.com", "max_pages": 3}
                )
            ]
        ),
        # 6. tool-cycle terminating text (discarded — Phase 7 follow_up ends on a forced
        # scoped create_text answer, not this mid-cycle text)
        StubResponse(content=[TextBlock("Pricing page crawled.")]),
        # 7. forced scoped conversational answer (create_text, Phase 7) citing the new claim
        StubResponse(
            content=[TextBlock("Per-seat pricing is $6/user on the base plan [CLM-003].")]
        ),
    ]


def _run_with_followup() -> tuple:
    transport = ReplayTransport(_fixtures())
    tools = [CrawlSiteTool(transport=transport), MetaAdsTool(transport=transport)]
    client = StubClient(sonnet_script=_initial_script(), haiku_text=HAIKU_CLAIMS)

    asked: list[str] = []

    def ask(q: str) -> str:
        asked.append(q)
        return "SMB focus"

    result = asyncio.run(run_competitor("gusto.com", client, tools, ask_user=ask))

    # swap the extractor output for the follow-up turn (same stub client)
    client.messages._haiku = StubResponse(content=[TextBlock(PRICING_CLAIM)])
    thread_len_before = len(result.session.messages)
    answer = asyncio.run(follow_up(result.session, "dig deeper on pricing", client, tools))
    return result, answer, asked, thread_len_before


def test_ask_user_called_exactly_once_with_the_question():
    result, _answer, asked, _ = _run_with_followup()
    assert asked == ["Should I focus SMB, enterprise, or both?"]
    assert result.clarifying_question == "Should I focus SMB, enterprise, or both?"
    # the real interactive answer landed in the thread
    assert {"role": "user", "content": "SMB focus"} in result.session.messages


def test_follow_up_appends_to_live_thread_not_a_fresh_conversation():
    result, _answer, _asked, before = _run_with_followup()
    msgs = result.session.messages
    assert len(msgs) > before  # grew, never reset
    assert msgs[0] == {"role": "user", "content": "analyze gusto.com"}  # original intake intact
    # Phase 7: the follow-up user message now carries the ledger digest alongside the
    # question, so match by substring rather than exact dict equality.
    followup_user = [
        m
        for m in msgs
        if m["role"] == "user"
        and isinstance(m["content"], str)
        and "dig deeper on pricing" in m["content"]
    ]
    assert followup_user, "follow-up question must land on the live thread"


def test_follow_up_grows_same_ledger_with_continuing_ids():
    result, answer, _asked, _ = _run_with_followup()
    ids = [c.id for c in result.session.ledger]
    assert ids[:2] == ["CLM-001", "CLM-002"]  # from the initial run
    assert "CLM-003" in ids  # follow-up claim continued the counter, same ledger
    assert "[CLM-003]" in answer  # grounded answer cites the new claim


def test_step_budget_is_shared_across_follow_ups():
    result, _answer, _asked, _ = _run_with_followup()
    # 1 tool call in the initial run + 1 in the follow-up, same session counter
    assert result.session.steps == 2


def test_new_competitor_gets_fresh_session():
    transport = ReplayTransport(_fixtures())
    tools = [CrawlSiteTool(transport=transport)]
    script = [
        StubResponse(content=[TextBlock("SMB or enterprise?")]),
        StubResponse(
            content=[
                ToolUseBlock(
                    id="t1", name="crawl_site", input={"domain": "gusto.com", "max_pages": 3}
                )
            ]
        ),
        StubResponse(content=[TextBlock("Research looks sufficient.")]),
        StubResponse(content=[TextBlock("Brief [CLM-001].")]),
    ]
    client = StubClient(sonnet_script=list(script), haiku_text=HAIKU_CLAIMS)
    r1 = asyncio.run(run_competitor("gusto.com", client, tools, clarifying_answer="both"))

    client2 = StubClient(sonnet_script=list(script), haiku_text=HAIKU_CLAIMS)
    r2 = asyncio.run(run_competitor("deel.com", client2, tools, clarifying_answer="both"))

    assert r1.session is not r2.session
    assert r2.session.messages[0] == {"role": "user", "content": "analyze deel.com"}
    assert r2.session.ledger[0].id == "CLM-001"  # counter restarted — fresh ledger
    assert r2.session.ledger[0].competitor == "deel.com"

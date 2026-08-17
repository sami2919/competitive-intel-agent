"""Phase 7 follow-up behavior tests — the mechanism that prevents the regen bug.

The stubbed test_session suite (lessons.md meta-lesson) could not catch the original bug
because it scripted a one-line answer. These tests assert the MECHANISM: follow_up uses
the followup_v1 prompt (never orchestrator_v2), surfaces the ledger digest, ends on a
prose-only create_text call (no tools, thinking disabled), defends on an empty ledger,
and — the regression guard — returns the forced scoped answer even when the tool cycle
emits a full-brief-shaped text. The live acceptance gate (Layer 4) guards the real model
behavior these stubs can't reproduce.
"""

from __future__ import annotations

import asyncio

from agent.loop import follow_up
from agent.session import Session
from evals.stub import StubClient, StubResponse, TextBlock, ToolUseBlock
from evals.test_grounding import _claim


def _msg_text(content: object) -> str:
    """Flatten a message's content to text — handles str, a list of TextBlock blocks
    (assistant turns, where resp.content is a block list), and tool_result dicts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, dict):
                parts.append(str(block.get("content", "")))
        return "".join(parts)
    return ""


def _session_with_ledger() -> Session:
    """A session that already has a priced claim + a sub-gate claim (so follow_up doesn't
    early-return and grounding has something to check)."""
    s = Session(competitor="gusto.com")
    s.ledger = [
        _claim("CLM-001", 0.7, category="pricing", statement="Gusto charges a flat $6/user rate"),
        _claim("CLM-002", 0.3, category="icp_targeting", statement="Gusto targets solopreneurs"),
    ]
    s.claim_counter = [2]
    return s


def _sonnet_calls(client: StubClient) -> list[dict]:
    return [c for c in client.messages.calls if "haiku" not in c["model"]]


# --- the regression guard --------------------------------------------------


def test_follow_up_does_not_regen_full_brief():
    """The tool cycle emits a full 6-section brief (simulating the old regen behavior);
    follow_up must return the forced scoped create_text answer instead."""
    session = _session_with_ledger()
    script = [
        # index 0: planning turn — model immediately writes a FULL BRIEF (no tool_use)
        StubResponse(
            content=[
                TextBlock(
                    "## What's Winning\n- Gusto flat-rate pricing [CLM-001].\n"
                    "## What Changed Recently\n(none)\n"
                    "## Rippling-relevance\n- angle [CLM-001]"
                )
            ]
        ),
        # index 1: the forced scoped conversational answer (create_text)
        StubResponse(
            content=[TextBlock("On pricing: Gusto charges a flat $6/user rate [CLM-001].")]
        ),
    ]
    client = StubClient(sonnet_script=script, haiku_text="[]")
    answer = asyncio.run(follow_up(session, "dig deeper on pricing", client, tools=[]))

    # the returned answer is the scoped one (index 1), NOT the brief from the cycle (index 0)
    assert "## What's Winning" not in answer
    assert "## Rippling-relevance" not in answer
    assert "[CLM-001]" in answer  # still cited
    # and the brief the model emitted mid-cycle IS in the thread (proving the test bites:
    # if create_text were removed, `answer` would be that brief and the asserts above fail)
    assistant_text = [_msg_text(m["content"]) for m in session.messages if m["role"] == "assistant"]
    assert any("## What's Winning" in t for t in assistant_text)


# --- the mechanism ---------------------------------------------------------


def test_follow_up_uses_followup_prompt_not_orchestrator():
    session = _session_with_ledger()
    script = [
        StubResponse(content=[TextBlock("Pricing: flat rate [CLM-001].")]),
        StubResponse(content=[TextBlock("Pricing: flat rate [CLM-001].")]),
    ]
    client = StubClient(sonnet_script=script, haiku_text="[]")
    asyncio.run(follow_up(session, "dig deeper on pricing", client, tools=[]))

    calls = _sonnet_calls(client)
    assert calls, "expected at least one sonnet call"
    for c in calls:
        assert "follow-up" in c["system_text"].lower()
        assert "always write the full brief" not in c["system_text"].lower()


def test_follow_up_final_call_is_prose_only():
    """The last sonnet call is the forced answer: create_text (tools=[], thinking disabled).
    If someone removes that call, the last call would be a tool-use create (tools=non-empty)."""
    session = _session_with_ledger()
    script = [
        StubResponse(content=[TextBlock("ok")]),
        StubResponse(content=[TextBlock("Pricing: flat rate [CLM-001].")]),
    ]
    client = StubClient(sonnet_script=script, haiku_text="[]")
    asyncio.run(follow_up(session, "dig deeper on pricing", client, tools=[]))

    last = _sonnet_calls(client)[-1]
    assert last["tools"] == []
    assert last["thinking"] == {"type": "disabled"}


def test_follow_up_surfaces_ledger_digest_in_user_message():
    session = _session_with_ledger()
    script = [
        StubResponse(content=[TextBlock("ok")]),
        StubResponse(content=[TextBlock("Pricing [CLM-001].")]),
    ]
    client = StubClient(sonnet_script=script, haiku_text="[]")
    asyncio.run(follow_up(session, "dig deeper on pricing", client, tools=[]))

    user_msgs = [
        m for m in session.messages if m["role"] == "user" and isinstance(m["content"], str)
    ]
    assert any("Current claims ledger" in m["content"] for m in user_msgs)
    assert any("[CLM-001]" in m["content"] for m in user_msgs)  # the digest is in the turn


def test_follow_up_empty_ledger_defends_without_calling_model():
    session = Session(competitor="gusto.com")  # empty ledger
    client = StubClient(
        sonnet_script=[StubResponse(content=[TextBlock("should not be reached")])],
        haiku_text="[]",
    )
    answer = asyncio.run(follow_up(session, "dig deeper on pricing", client, tools=[]))

    assert "analyze" in answer.lower()  # the "run analyze first" guidance
    assert client.messages.calls == []  # no model call was made


def test_follow_up_can_call_a_tool_after_a_big_initial_run():
    """Regression (code-review P1 #1): _tool_cycle's guard is `session.steps < max_steps`,
    but session.steps is cumulative. A follow-up after a many-tool initial run must still
    be able to call a tool. The stubbed suite's initial run used only 1 step, so `1 < 4`
    passed and hid that `8 < 4` would silently drop every follow-up tool call."""
    from evals.test_loop import HAIKU_CLAIMS, _fixtures
    from tools._transport import ReplayTransport
    from tools.crawl_site import CrawlSiteTool

    session = _session_with_ledger()
    session.steps = 10  # simulate a big initial run — the bug condition
    transport = ReplayTransport(_fixtures())
    tools = [CrawlSiteTool(transport=transport)]
    script = [
        StubResponse(
            content=[
                ToolUseBlock(
                    id="t1", name="crawl_site", input={"domain": "gusto.com", "max_pages": 3}
                )
            ]
        ),
        StubResponse(content=[TextBlock("Pricing page crawled.")]),
        StubResponse(content=[TextBlock("On pricing: flat rate [CLM-001].")]),
    ]
    client = StubClient(sonnet_script=script, haiku_text=HAIKU_CLAIMS)
    asyncio.run(follow_up(session, "dig deeper on pricing", client, tools))

    assert session.steps == 11  # the follow-up tool call was actually dispatched

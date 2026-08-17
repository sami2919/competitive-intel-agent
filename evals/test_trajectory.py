"""Trajectory evals — tool-call trace invariants (CLAUDE.md §7 Layer 3).

Asserts on trajectory invariants:
  - ONE clarifying question before any tool call (result.clarifying_question
    non-empty, first scripted response is a TextBlock, not a ToolUseBlock)
  - Step budget enforcement (hard cap at max_steps, not exceeded)
  - Skip-empty: a source returning status='empty' is noted and the loop
    continues without crashing or infinite-looping
  - Unknown tool names produce an is_error result and cause no crash
  - Multiple ToolUseBlocks in one API response are all counted toward steps

These are Layer-3 evals: they verify the orchestrator's tool-use discipline
under scripted conditions. Runs offline with ReplayTransport + StubClient
(no API keys, no network).  URL health rationale (arXiv 2604.03173) is
handled by the url_health validator; trajectory evals focus on tool-call
trace invariants.

The orchestrator's step budget and skip-empty logic are the primary guard
against the "gambler's ruin" failure mode: each agent turn is
probabilistic, so chaining unbounded tool calls compounds failure odds.
"""

from __future__ import annotations

import asyncio
import json

from agent.loop import run_competitor
from evals.stub import StubClient, StubResponse, TextBlock, ToolUseBlock
from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.crawl_site import CrawlSiteTool

FIRECRAWL = "https://api.firecrawl.dev/v1"

SIMPLE_HAIKU = json.dumps(
    [
        {
            "statement": "Gusto leads with simple flat-rate pricing",
            "category": "pricing",
            "source_url": "https://gusto.com/pricing",
            "excerpt": "Pricing from $39/mo + $6/user.",
        },
    ]
)


def _ok_fixtures() -> dict[str, HttpResponse]:
    """Normal crawl_site response: /map returns links, /scrape returns markdown."""
    f: dict[str, HttpResponse] = {}
    f[HttpRequest("POST", f"{FIRECRAWL}/map", json_body={"url": "https://gusto.com"}).key()] = (
        HttpResponse(200, json.dumps({"links": ["https://gusto.com", "https://gusto.com/pricing"]}))
    )
    f[
        HttpRequest(
            "POST",
            f"{FIRECRAWL}/scrape",
            json_body={"url": "https://gusto.com", "formats": ["markdown"]},
        ).key()
    ] = HttpResponse(200, json.dumps({"data": {"markdown": "# Gusto — simple payroll"}}))
    f[
        HttpRequest(
            "POST",
            f"{FIRECRAWL}/scrape",
            json_body={"url": "https://gusto.com/pricing", "formats": ["markdown"]},
        ).key()
    ] = HttpResponse(200, json.dumps({"data": {"markdown": "Pricing from $39/mo"}}))
    return f


def _empty_fixtures() -> dict[str, HttpResponse]:
    """Fixture that causes crawl_site to return status='empty' (no links found)."""
    return {
        HttpRequest(
            "POST", f"{FIRECRAWL}/map", json_body={"url": "https://gusto.com"}
        ).key(): HttpResponse(200, json.dumps({"links": []})),
    }


# ---- tests ----------------------------------------------------------------


def test_clarifying_question_captured():
    """The orchestrator must ask exactly ONE clarifying question before calling any tool.

    The first scripted response is a TextBlock (the question), so no tool is
    dispatched on the intake turn.  The answer is fed back, and the loop
    proceeds — but no tool calls are made on the first pass.
    """
    script = [
        StubResponse(content=[TextBlock("Should I focus on SMB, enterprise, or both?")]),
        StubResponse(content=[TextBlock("Research looks sufficient.")]),
        # consumed by the loop's dedicated explicit "write the brief" synthesis call
        StubResponse(content=[TextBlock("## Brief\nNo tools called.")]),
    ]
    transport = ReplayTransport({})
    tools = [CrawlSiteTool(transport=transport)]
    client = StubClient(sonnet_script=script, haiku_text=SIMPLE_HAIKU)

    result = asyncio.run(run_competitor("gusto.com", client, tools, clarifying_answer="both"))

    assert result.clarifying_question
    # The question must reference at least one of the relevant audience dimensions
    assert "SMB" in result.clarifying_question or "enterprise" in result.clarifying_question
    assert result.steps == 0  # no tool calls before the question


def test_step_budget_respected():
    """The loop must stop at max_steps when the model keeps requesting tools.

    Even with a script that always returns ToolUseBlock, the step counter
    must never exceed max_steps (here 3).
    """
    script = [
        StubResponse(
            content=[ToolUseBlock(id=f"t{i}", name="crawl_site", input={"domain": "gusto.com"})]
        )
        for i in range(10)
    ]
    script.append(StubResponse(content=[TextBlock("## Brief\nBudget reached.")]))

    transport = ReplayTransport(_ok_fixtures())
    tools = [CrawlSiteTool(transport=transport)]
    client = StubClient(sonnet_script=script, haiku_text=SIMPLE_HAIKU)

    result = asyncio.run(
        run_competitor("gusto.com", client, tools, clarifying_answer="both", max_steps=3)
    )

    assert result.steps == 3  # hard cap, not 10


def test_skip_empty_source():
    """A tool returning status='empty' must be noted and the loop continues.

    When crawl_site returns empty (no link data), _dispatch emits a digest
    saying so and returns no claims.  The loop must not crash, must not
    loop forever, and must proceed to the next turn.
    """
    script = [
        StubResponse(content=[TextBlock("SMB or enterprise?")]),
        StubResponse(
            content=[
                ToolUseBlock(
                    id="t1",
                    name="crawl_site",
                    input={"domain": "gusto.com", "max_pages": 3},
                )
            ]
        ),
        StubResponse(content=[TextBlock("## Brief\nCrawl returned nothing.")]),
    ]
    transport = ReplayTransport(_empty_fixtures())
    tools = [CrawlSiteTool(transport=transport)]
    client = StubClient(sonnet_script=script, haiku_text=SIMPLE_HAIKU)

    result = asyncio.run(run_competitor("gusto.com", client, tools, clarifying_answer="both"))

    assert result.steps == 1  # the empty crawl_site call was counted
    assert result.brief  # loop continued past empty


def test_unknown_tool_no_crash():
    """An unrecognized tool name must produce an is_error result and the loop must continue.

    When the orchestrator requests a tool not in the registry, the loop
    emits an is_error tool_result, increments the step counter, and
    proceeds to the next turn.  It must never crash or hang.
    """
    script = [
        StubResponse(content=[TextBlock("SMB or enterprise?")]),
        StubResponse(content=[ToolUseBlock(id="t1", name="nonexistent_tool", input={})]),
        StubResponse(
            content=[
                ToolUseBlock(
                    id="t2",
                    name="crawl_site",
                    input={"domain": "gusto.com", "max_pages": 3},
                )
            ]
        ),
        StubResponse(content=[TextBlock("## Brief\nUnknown tool handled.")]),
    ]
    transport = ReplayTransport(_ok_fixtures())
    tools = [CrawlSiteTool(transport=transport)]
    client = StubClient(sonnet_script=script, haiku_text=SIMPLE_HAIKU)

    result = asyncio.run(run_competitor("gusto.com", client, tools, clarifying_answer="both"))

    assert result.steps == 2  # nonexistent_tool + crawl_site
    assert result.brief


def test_multiple_tools_in_one_turn():
    """Multiple ToolUseBlocks in one response must all be processed before the next API call.

    The for loop inside the while cycle iterates all ToolUseBlocks from a
    single response and calls _dispatch for each.  All of them count toward
    the step total.
    """
    script = [
        StubResponse(content=[TextBlock("SMB or enterprise?")]),
        StubResponse(
            content=[
                ToolUseBlock(
                    id="t1",
                    name="crawl_site",
                    input={"domain": "gusto.com", "max_pages": 3},
                ),
                ToolUseBlock(
                    id="t2",
                    name="crawl_site",
                    input={"domain": "gusto.com", "max_pages": 3},
                ),
            ]
        ),
        StubResponse(content=[TextBlock("## Brief\nTwo tools called.")]),
    ]
    transport = ReplayTransport(_ok_fixtures())
    tools = [CrawlSiteTool(transport=transport)]
    client = StubClient(sonnet_script=script, haiku_text=SIMPLE_HAIKU)

    result = asyncio.run(run_competitor("gusto.com", client, tools, clarifying_answer="both"))

    assert result.steps == 2  # both tool blocks counted
    assert result.brief

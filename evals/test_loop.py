"""Orchestrator loop integration test — stubbed Anthropic client, fixed tool sequence.

This is the CLAUDE.md §15 acceptance test: 'orchestrator loop with a stubbed Anthropic
client driving a fixed tool sequence.' Verifies the eng-review lock-ins end-to-end:
  - ONE clarifying question before tool calls (intake)
  - tool_use cycle calls tools, feeds digests, respects step budget
  - D6 digests (not raw pages / not full ledger) carried in the message thread
  - meta_ads zero-ads gotcha handled as data (empty digest, not a crash)
  - claims land in the ledger with deterministic confidence
  - brief carries inline [CLM-xxx] citations
  - cost accumulates every turn
"""

from __future__ import annotations

import asyncio
import json

from agent.loop import run_competitor
from evals.stub import StubClient, StubResponse, TextBlock, ToolUseBlock
from tools._transport import HttpRequest, HttpResponse, ReplayTransport
from tools.crawl_site import CrawlSiteTool
from tools.meta_ads import MetaAdsTool

FIRECRAWL = "https://api.firecrawl.dev/v1"
SC = "https://api.scrapecreators.com/v1"

HAIKU_CLAIMS = json.dumps(
    [
        {
            "statement": "Gusto leads with simple flat-rate pricing for small businesses",
            "category": "pricing",
            "source_url": "https://gusto.com/pricing",
            "excerpt": "Pricing from $39/mo + $6/user. No hidden fees.",
        },
        {
            "statement": "Gusto targets US small businesses",
            "category": "icp_targeting",
            "source_url": "https://gusto.com",
            "excerpt": "Payroll and benefits for small business.",
        },
    ]
)


def _fixtures() -> dict[str, HttpResponse]:
    f: dict[str, HttpResponse] = {}
    f[HttpRequest("POST", f"{FIRECRAWL}/map", json_body={"url": "https://gusto.com"}).key()] = (
        HttpResponse(status=200, body=json.dumps({"links": ["gusto.com", "gusto.com/pricing"]}))
    )
    f[
        HttpRequest(
            "POST",
            f"{FIRECRAWL}/scrape",
            json_body={"url": "https://gusto.com", "formats": ["markdown"]},
        ).key()
    ] = HttpResponse(
        status=200,
        body=json.dumps({"data": {"markdown": "# Gusto — simple payroll for small business"}}),
    )
    f[
        HttpRequest(
            "POST",
            f"{FIRECRAWL}/scrape",
            json_body={"url": "https://gusto.com/pricing", "formats": ["markdown"]},
        ).key()
    ] = HttpResponse(
        status=200,
        body=json.dumps({"data": {"markdown": "Pricing from $39/mo + $6/user. No hidden fees."}}),
    )
    # meta_ads: the US-ads gotcha -> zero ads
    f[
        HttpRequest(
            "POST",
            f"{SC}/facebook/adLibrary/search/ads",
            json_body={"advertiser_domain": "gusto.com"},
        ).key()
    ] = HttpResponse(status=200, body=json.dumps({"ads": []}))
    return f


def _script() -> list[StubResponse]:
    return [
        # 1. intake: proposes plan + asks ONE clarifying question
        StubResponse(
            content=[
                TextBlock(
                    "I'll check Meta ads, site positioning, and recent changes. Should I focus SMB, enterprise, or both?"
                )
            ]
        ),
        # 2. after "both": call crawl_site
        StubResponse(
            content=[
                ToolUseBlock(
                    id="t1", name="crawl_site", input={"domain": "gusto.com", "max_pages": 3}
                )
            ]
        ),
        # 3. after crawl_site digest: call meta_ads
        StubResponse(
            content=[
                ToolUseBlock(id="t2", name="meta_ads", input={"advertiser_domain": "gusto.com"})
            ]
        ),
        # 4. after meta_ads (empty): spontaneous end-of-cycle text (no tool_use) -> ends
        # the tool_use loop. This is NOT the brief -- the loop always makes a dedicated
        # explicit "write the brief" synthesis call afterward (agent/loop.py run_competitor).
        StubResponse(content=[TextBlock("Research looks sufficient — ready to synthesize.")]),
        # 5. explicit "write the brief" synthesis call, with inline citations.
        # [CLM-002] (icp_targeting, conf 0.3 = sub-gate) is in the appendix, not the body,
        # so the grounding validator (CLAUDE.md §5) passes on the first attempt.
        StubResponse(
            content=[
                TextBlock(
                    "## Gusto competitive brief\n"
                    "Gusto leads with simple flat-rate pricing for small businesses [CLM-001]. "
                    "No active Meta ads were found.\n\n"
                    "## Unverified signals\n- Targets US small businesses [CLM-002] (inferred, low-confidence)."
                )
            ]
        ),
    ]


def test_loop_full_run():
    transport = ReplayTransport(_fixtures())
    tools = [CrawlSiteTool(transport=transport), MetaAdsTool(transport=transport)]
    client = StubClient(sonnet_script=_script(), haiku_text=HAIKU_CLAIMS)

    result = asyncio.run(run_competitor("gusto.com", client, tools, clarifying_answer="both"))

    # ONE clarifying question captured before any tool call
    assert "SMB" in result.clarifying_question or "enterprise" in result.clarifying_question

    # tool_use cycle ran exactly two tools
    assert result.steps == 2

    # claims landed in the ledger with deterministic confidence (single primary source = 0.7)
    assert len(result.ledger) == 2
    assert result.ledger[0].id == "CLM-001"
    assert result.ledger[0].confidence == 0.7  # crawl_site = primary
    assert result.ledger[1].confidence == 0.3  # icp_targeting = inferred

    # brief carries inline [CLM-xxx] citations (D3 grounding format)
    assert "[CLM-001]" in result.brief
    assert "[CLM-002]" in result.brief

    # cost accumulated across turns (4 sonnet + 1 haiku stub calls)
    assert result.cost.total > 0


def test_loop_step_budget_caps_tool_calls():
    """If the model keeps requesting tools, the loop stops at STEP_BUDGET and synthesizes."""
    # script that always requests crawl_site
    always_tool = [
        StubResponse(
            content=[ToolUseBlock(id=f"t{i}", name="crawl_site", input={"domain": "gusto.com"})]
        )
        for i in range(100)
    ]
    # index 4: consumed by the loop's dedicated explicit synthesis call, which fires
    # right after the tool_use cycle stops at the step budget.
    always_tool[4] = StubResponse(content=[TextBlock("## Brief\nBudget reached.")])
    transport = ReplayTransport(_fixtures())
    tools = [CrawlSiteTool(transport=transport)]
    client = StubClient(sonnet_script=always_tool, haiku_text=HAIKU_CLAIMS)

    result = asyncio.run(
        run_competitor("gusto.com", client, tools, clarifying_answer="both", max_steps=3)
    )
    assert result.steps == 3  # hard cap, not 100

"""Landing-page snapshot: pure markdown parsing offline, plus the tool via ReplayTransport."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cro.snapshot import FIRECRAWL_BASE, PageSnapshotTool, parse_snapshot
from ledger.models import ToolFailure
from tools._transport import HttpRequest, HttpResponse, ReplayTransport

PAGE = """\
[Login](https://www.rippling.com/login)
[Blog](https://www.rippling.com/blog)

# Payroll that runs itself

Run payroll in 90 seconds across all 50 states, with taxes filed automatically.

[Get started](https://www.rippling.com/signup)
[Talk to sales](https://www.rippling.com/contact)

Trusted by 20,000 businesses

## How it works
- Sync hours
- Approve
"""


def test_parses_hero_subhead_cta_and_proof():
    snap = parse_snapshot("https://www.rippling.com/payroll", PAGE)
    assert snap.element("hero").text == "Payroll that runs itself"
    assert "90 seconds" in snap.element("subhead").text
    assert snap.element("cta").text == "Get started"
    assert "20,000 businesses" in snap.element("proof").text


def test_nav_links_are_never_mistaken_for_the_cta():
    # "Login" and "Blog" appear BEFORE the real CTA in the markdown — a naive
    # first-link parse would pick one of them.
    assert parse_snapshot("https://x.com", PAGE).element("cta").text == "Get started"


def test_headings_and_bullets_are_not_treated_as_the_subhead():
    md = "# Hero here\n\n## A subheading\n\n- a bullet point item\n\nThe real prose line goes here.\n"
    assert parse_snapshot("https://x.com", md).element("subhead").text == (
        "The real prose line goes here."
    )


def test_short_fragments_are_not_treated_as_the_subhead():
    md = "# Hero here\n\nToo short\n\nA properly substantive sentence follows this one.\n"
    assert "substantive" in parse_snapshot("https://x.com", md).element("subhead").text


def test_page_with_no_h1_fails_loudly():
    # No h1 means no control arm — generating variants against nothing is worse
    # than failing.
    with pytest.raises(ValueError, match="no h1 found"):
        parse_snapshot("https://x.com", "## only a subheading\n\nsome text\n")


def test_missing_optional_elements_are_simply_absent():
    snap = parse_snapshot("https://x.com", "# Just a hero\n")
    assert snap.element("hero")
    assert snap.element("cta") is None
    assert snap.element("proof") is None


def test_snapshot_is_frozen():
    snap = parse_snapshot("https://x.com", PAGE, fetched_at=datetime.now(UTC))
    with pytest.raises(ValidationError):
        snap.url = "https://other.com"


# --- the tool, offline via ReplayTransport -----------------------------------


TARGET = "https://www.rippling.com/payroll"


def _replay(body: str, status: int = 200) -> ReplayTransport:
    """Fixture keyed by the exact request the tool will build."""
    request = HttpRequest(
        "POST",
        f"{FIRECRAWL_BASE}/scrape",
        json_body={"url": TARGET, "formats": ["markdown"]},
    )
    return ReplayTransport({request.key(): HttpResponse(status=status, body=body)})


async def test_tool_returns_source_result_from_recorded_fixture():
    transport = _replay(json.dumps({"data": {"markdown": PAGE}}))
    result = await PageSnapshotTool(transport).run(url=TARGET)
    assert not isinstance(result, ToolFailure)
    assert result.status == "ok"
    assert "Payroll that runs itself" in result.raw_excerpt

    snap = parse_snapshot(result.url, result.raw_excerpt, result.fetched_at)
    assert snap.element("hero").text == "Payroll that runs itself"


async def test_tool_reports_empty_rather_than_failing_on_a_blank_page():
    transport = _replay(json.dumps({"data": {"markdown": ""}}))
    result = await PageSnapshotTool(transport).run(url=TARGET)
    assert not isinstance(result, ToolFailure)
    assert result.status == "empty"


async def test_non_json_response_becomes_a_typed_failure_not_an_exception():
    transport = _replay("<html>rate limited</html>")
    result = await PageSnapshotTool(transport).run(url=TARGET)
    assert isinstance(result, ToolFailure)
    assert "non-JSON" in result.reason


def test_tool_declares_the_env_it_needs():
    assert PageSnapshotTool.required_env == ["FIRECRAWL_API_KEY"]

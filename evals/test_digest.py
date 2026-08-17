"""Digest source-attribution tests — the synthesis model must be able to see which
tool produced each claim (the 2026-07-28 live run wrote 'no wayback_diff claims were
returned' while the source table showed 2, because the digest omitted source_tool)."""

from __future__ import annotations

import inspect
import io

from rich.console import Console

from agent.digest import claim_digest_line, digest_preview, source_coverage_line
from agent.loop import _cached_system, _cached_tools
from evals.test_grounding import _claim


def _sourced(cid: str, tool: str, confidence: float = 0.7):
    return _claim(cid, confidence).model_copy(update={"source_tool": tool})


def test_claim_digest_line_includes_source_tool():
    line = claim_digest_line(_sourced("CLM-066", "wayback_diff"))
    assert "via wayback_diff" in line


def test_claim_digest_line_no_via_when_unsourced():
    line = claim_digest_line(_claim("CLM-001", 0.7))  # source_tool defaults to ""
    assert "via " not in line


def test_source_coverage_line_counts_per_tool():
    ledger = [
        _sourced("CLM-001", "crawl_site"),
        _sourced("CLM-002", "crawl_site"),
        _sourced("CLM-066", "wayback_diff"),
    ]
    line = source_coverage_line(ledger)
    assert line.startswith("Source coverage (deterministic, from the ledger):")
    assert "crawl_site: 2 claims" in line
    assert "wayback_diff: 1 claim" in line


def test_source_coverage_line_empty_ledger():
    assert source_coverage_line([]) == ""


# --- cache breakpoints (Task 2) ---------------------------------------------

CACHE_1H = {"type": "ephemeral", "ttl": "1h"}


def test_cached_system_uses_1h_ttl():
    blocks = _cached_system("prompt body")
    assert blocks == [{"type": "text", "text": "prompt body", "cache_control": CACHE_1H}]


def test_cached_tools_marks_last_schema_with_1h_ttl():
    schemas = [{"name": "a"}, {"name": "b"}]
    cached = _cached_tools(schemas)
    assert "cache_control" not in cached[0]
    assert cached[1]["cache_control"] == CACHE_1H
    assert schemas[1] == {"name": "b"}  # originals never mutated


# --- progress previews (Task 3) ---------------------------------------------


def test_digest_preview_first_line_only():
    digest = "[CLM-001] (positioning) first claim\n[CLM-002] (messaging) second claim"
    preview = digest_preview(digest)
    assert "\n" not in preview
    assert "CLM-002" not in preview


def test_digest_preview_truncates_with_ellipsis():
    line = "[CLM-001] (positioning) " + "x" * 200
    preview = digest_preview(line, width=80)
    assert len(preview) == 80
    assert preview.endswith("…")


def test_digest_preview_short_line_untouched():
    assert digest_preview("empty — no data returned from this source") == (
        "empty — no data returned from this source"
    )


# --- markup-safe printing (Task 4) ------------------------------------------


def test_brief_prose_survives_console_print():
    """[evidence] is a valid-looking lowercase rich tag; with markup enabled it is
    swallowed (observed live: "we believe X because ' bets"). The REPL must print
    briefs/answers with markup=False."""
    buf = io.StringIO()
    test_console = Console(file=buf, width=200)
    prose = "ranked 'we believe X because [evidence]' bets [CLM-033]"
    test_console.print(prose, markup=False)
    out = buf.getvalue()
    assert "[evidence]" in out
    assert "[CLM-033]" in out


def test_repl_prints_prose_with_markup_disabled():
    import agent.repl as repl

    source = inspect.getsource(repl)
    assert source.count("markup=False") >= 3  # brief + compare answer + follow-up answer


# --- cached prefix on prose-only calls (Task 5) ------------------------------
from agent.llm import create_text  # noqa: E402
from evals.stub import StubClient, StubResponse, TextBlock  # noqa: E402


def test_create_text_with_tools_forbids_tool_use_but_keeps_prefix():
    client = StubClient(sonnet_script=[StubResponse(content=[TextBlock("ok")])], haiku_text="[]")
    schemas = [{"name": "crawl_site"}, {"name": "meta_ads"}]
    create_text(client, "claude-sonnet-5", "sys", [{"role": "user", "content": "x"}], tools=schemas)
    call = client.messages.calls[-1]
    assert call["tools"] == schemas
    assert call["tool_choice"] == {"type": "none"}
    assert call["thinking"] == {"type": "disabled"}


def test_create_text_without_tools_unchanged():
    client = StubClient(sonnet_script=[StubResponse(content=[TextBlock("ok")])], haiku_text="[]")
    create_text(client, "claude-sonnet-5", "sys", [{"role": "user", "content": "x"}])
    call = client.messages.calls[-1]
    assert call["tools"] == []
    assert call["tool_choice"] is None

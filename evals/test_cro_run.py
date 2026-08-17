"""CRO run orchestration — full pipeline offline via ReplayTransport + stub client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cro.run import CroRunResult, page_slug, run_cro
from cro.snapshot import FIRECRAWL_BASE
from evals.stub import StubResponse, StubUsage, TextBlock
from tools._transport import HttpRequest, HttpResponse, ReplayTransport

PAGE_URL = "https://www.rippling.com/payroll"
PAGE_MD = (
    "# Payroll that runs itself\n\n"
    "Run payroll in 90 seconds across all 50 states.\n\n"
    "[Get started](https://www.rippling.com/signup)\n"
)
MODEL_JSON = json.dumps(
    [
        {
            "headline": "Simplicity that survives 50 states",
            "subhead": "Run payroll across every US state.",
            "cta": "See how",
            "changed_elements": ["hero"],
            "claim_refs": ["CAN-009"],
            "segment": "30-200 employee migration",
        },
        {
            "headline": "Cheaper than Gusto",
            "subhead": "",
            "cta": "See how",
            "changed_elements": ["hero"],
            "claim_refs": [],
            "segment": "30-200 employee migration",
        },
    ]
)


def _transport(body: str, status: int = 200) -> ReplayTransport:
    request = HttpRequest(
        "POST", f"{FIRECRAWL_BASE}/scrape", json_body={"url": PAGE_URL, "formats": ["markdown"]}
    )
    return ReplayTransport({request.key(): HttpResponse(status=status, body=body)})


class _StubClient:
    """Returns the same variant JSON for every generation call, recording each."""

    def __init__(self, text: str = MODEL_JSON) -> None:
        self.messages = self
        self._text = text
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return StubResponse(content=[TextBlock(text=self._text)], usage=StubUsage())


@pytest.fixture
def facts_file(tmp_path: Path) -> Path:
    path = tmp_path / "facts.yaml"
    path.write_text(
        """
facts:
  - id: FACT-005
    statement: "Rippling supports multi-state US payroll across 50 states"
    category: coverage
    source_url: "https://www.rippling.com/payroll"
    verified: true
"""
    )
    return path


def test_page_slug_is_filesystem_safe():
    assert page_slug("https://www.rippling.com/payroll") == "rippling-payroll"
    assert page_slug("https://www.rippling.com/") == "rippling-home"


async def test_full_run_writes_three_artifacts(tmp_path, facts_file):
    result = await run_cro(
        PAGE_URL,
        "gusto.com",
        transport=_transport(json.dumps({"data": {"markdown": PAGE_MD}})),
        client=_StubClient(),
        facts_path=facts_file,
        out_dir=tmp_path,
    )
    assert isinstance(result, CroRunResult)
    assert result.error == ""
    assert set(result.paths) == {"markdown", "json", "optimizely"}
    for path in result.paths.values():
        assert Path(path).exists()


async def test_run_uses_the_real_shipped_gusto_ledger(tmp_path, facts_file):
    result = await run_cro(
        PAGE_URL,
        "gusto.com",
        transport=_transport(json.dumps({"data": {"markdown": PAGE_MD}})),
        client=_StubClient(),
        facts_path=facts_file,
        out_dir=tmp_path,
    )
    # Hypotheses must trace to real CAN ids from outputs/gusto.com_canonical.json.
    assert result.hypotheses
    assert all(h.counters_canonical_id.startswith("CAN-") for h in result.hypotheses)


async def test_unsourced_variant_is_rejected_in_a_real_run(tmp_path, facts_file):
    result = await run_cro(
        PAGE_URL,
        "gusto.com",
        transport=_transport(json.dumps({"data": {"markdown": PAGE_MD}})),
        client=_StubClient(),
        facts_path=facts_file,
        out_dir=tmp_path,
    )
    assert any(s.reject_reason == "unsourced_claim" for s in result.scored)


async def test_low_traffic_page_caps_generation_before_spending_tokens(tmp_path, facts_file):
    client = _StubClient()
    result = await run_cro(
        PAGE_URL,
        "gusto.com",
        transport=_transport(json.dumps({"data": {"markdown": PAGE_MD}})),
        client=client,
        weekly_sessions=400,  # supports zero variants
        facts_path=facts_file,
        out_dir=tmp_path,
    )
    assert client.calls == []  # no model call at all
    assert "cannot support" in result.error


async def test_snapshot_failure_is_data_not_an_exception(tmp_path, facts_file):
    result = await run_cro(
        PAGE_URL,
        "gusto.com",
        transport=_transport("<html>rate limited</html>"),
        client=_StubClient(),
        facts_path=facts_file,
        out_dir=tmp_path,
    )
    assert result.error
    assert result.paths == {}
    assert result.scored == []


async def test_missing_ledger_is_reported_not_raised(tmp_path, facts_file):
    result = await run_cro(
        PAGE_URL,
        "nosuchcompetitor.com",
        transport=_transport(json.dumps({"data": {"markdown": PAGE_MD}})),
        client=_StubClient(),
        facts_path=facts_file,
        out_dir=tmp_path,
    )
    assert "no canonical ledger" in result.error


async def test_cost_line_reports_the_generator_model(tmp_path, facts_file):
    result = await run_cro(
        PAGE_URL,
        "gusto.com",
        transport=_transport(json.dumps({"data": {"markdown": PAGE_MD}})),
        client=_StubClient(),
        facts_path=facts_file,
        out_dir=tmp_path,
    )
    assert "Run complete" in result.cost_line
    assert "Sonnet: $" in result.cost_line


async def test_allowed_citations_include_ledger_and_verified_facts(tmp_path, facts_file):
    client = _StubClient()
    await run_cro(
        PAGE_URL,
        "gusto.com",
        transport=_transport(json.dumps({"data": {"markdown": PAGE_MD}})),
        client=client,
        facts_path=facts_file,
        out_dir=tmp_path,
    )
    sent = client.calls[0]["messages"][0]["content"]
    assert "CAN-" in sent
    assert "FACT-005" in sent


async def test_unverified_facts_never_reach_the_model(tmp_path):
    unverified = tmp_path / "unverified.yaml"
    unverified.write_text(
        """
facts:
  - id: FACT-003
    statement: "Rippling costs $8/user/month"
    category: pricing
    source_url: "https://www.rippling.com/blog/review"
    verified: false
"""
    )
    client = _StubClient()
    await run_cro(
        PAGE_URL,
        "gusto.com",
        transport=_transport(json.dumps({"data": {"markdown": PAGE_MD}})),
        client=client,
        facts_path=unverified,
        out_dir=tmp_path,
    )
    assert "FACT-003" not in client.calls[0]["messages"][0]["content"]


# --- CLI + mode-aware key validation ----------------------------------------


from cro.run import check_keys, parse_args  # noqa: E402


def test_parse_args_requires_a_page():
    args = parse_args(["--page", PAGE_URL])
    assert args.page == PAGE_URL
    assert args.competitor == "gusto.com"  # documented default


def test_parse_args_accepts_traffic_assumptions():
    args = parse_args(
        ["--page", PAGE_URL, "--competitor", "deel.com", "--sessions", "5000", "--cvr", "0.02"]
    )
    assert args.competitor == "deel.com"
    assert args.sessions == 5000
    assert args.cvr == 0.02


def test_live_mode_requires_the_firecrawl_key(monkeypatch):
    monkeypatch.setenv("INTEL_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert "FIRECRAWL_API_KEY" in check_keys()


def test_demo_mode_does_not_require_data_api_keys(monkeypatch):
    monkeypatch.setenv("INTEL_MODE", "demo")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert check_keys() == []


def test_anthropic_key_is_required_in_every_mode(monkeypatch):
    monkeypatch.setenv("INTEL_MODE", "demo")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "ANTHROPIC_API_KEY" in check_keys()


def test_main_exits_nonzero_without_keys(monkeypatch, capsys):
    # load_dotenv_first=False is load-bearing: with it True, load_env() repopulates
    # the keys this test just deleted and main() proceeds into a REAL paid run.
    monkeypatch.setenv("INTEL_MODE", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    from cro.run import main

    assert main(["--page", PAGE_URL], load_dotenv_first=False) == 1
    assert "Missing" in capsys.readouterr().out


def test_variant_ids_are_unique_across_hypotheses(tmp_path, facts_file):
    """Regression: each hypothesis used to restart numbering at VAR-001.

    Caught by reading the output of a live run — two variants both called VAR-001,
    which makes the JSON ambiguous and duplicates variation names in Optimizely.
    """
    import asyncio

    result = asyncio.run(
        run_cro(
            PAGE_URL,
            "gusto.com",
            transport=_transport(json.dumps({"data": {"markdown": PAGE_MD}})),
            client=_StubClient(),
            facts_path=facts_file,
            out_dir=tmp_path,
            max_hypotheses=3,
        )
    )
    ids = [s.variant.id for s in result.scored]
    assert len(ids) == len(set(ids)), f"duplicate variant ids: {ids}"

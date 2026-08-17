"""Layer-2 judge tests — stubbed call_llm, no network, no judge key required.

The judge's HTTP call is behind a `call_llm` callable, so tests inject a stub that
returns canned JSON. Covers: stratified sampling, JSON parsing (incl. fenced), score
parsing, aggregate math, file writing, and the no-key / no-ledger skip paths.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from evals.judge import (
    JudgeReport,
    _looks_like_real_key,
    _stratified_sample,
    judge_brief,
    judge_claims,
    judge_competitor,
    select_provider,
)
from ledger.models import Claim, Evidence

# --- provider selection / placeholder filtering -----------------------------


def test_looks_like_real_key_rejects_placeholders():
    assert not _looks_like_real_key("")
    assert not _looks_like_real_key("sk-...")  # .env.example placeholder
    assert not _looks_like_real_key("sk-...      # comment")
    assert not _looks_like_real_key("short")
    assert _looks_like_real_key("sk-" + "a" * 48)
    assert _looks_like_real_key("AIza" + "b" * 35)


def test_select_provider_skips_openai_placeholder_falls_back_to_gemini(monkeypatch):
    """The footgun: a copied .env.example has OPENAI_API_KEY=sk-... (non-empty placeholder)
    and a real GEMINI_API_KEY. select_provider must pick Gemini, not 401 on OpenAI."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-...")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "g" * 35)
    prov = select_provider()
    assert prov is not None
    assert prov[0] == "gemini"
    assert prov[1] == "gemini-3.5-flash"


def test_select_provider_none_when_both_placeholders(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-...")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert select_provider() is None


def test_select_provider_prefers_real_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "a" * 48)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "g" * 35)
    prov = select_provider()
    assert prov[0] == "openai"


def _claim(cid: str, conf: float, category: str = "messaging") -> Claim:
    return Claim(
        id=cid,
        competitor="gusto.com",
        category=category,
        statement=f"Claim {cid} is a specific numbered fact",
        evidence=[
            Evidence(
                source_url="https://gusto.com",
                excerpt=f"excerpt for {cid}",
                fetched_at=datetime.now(UTC),
            )
        ],
        confidence=conf,
        confidence_trace="trace",
        extracted_by="claude-haiku-4-5/extractor_v2",
        observed_vs_inferred="observed",
        signal=None,
        signal_trace="",
        canonical_id=None,
        source_tool="crawl_site",
    )


def _claims() -> list[Claim]:
    return [
        _claim("CLM-001", 0.9, "messaging"),
        _claim("CLM-002", 0.9, "pricing"),
        _claim("CLM-003", 0.7, "positioning"),
        _claim("CLM-004", 0.5, "recent_change"),
        _claim("CLM-005", 0.3, "ads_paid_social"),
        _claim("CLM-006", 0.3, "ads_search"),
    ]


# --- stratified sampling ---------------------------------------------------


def test_stratified_sample_covers_tiers():
    sample = _stratified_sample(_claims(), n=4)
    confs = [round(c.confidence, 2) for c in sample]
    # one per tier round-robin: >=0.9, 0.7, 0.5, <0.5
    assert 0.9 in confs
    assert 0.7 in confs
    assert 0.5 in confs
    assert 0.3 in confs
    assert len(sample) == 4


def test_stratified_sample_respects_cap():
    sample = _stratified_sample(_claims(), n=3)
    assert len(sample) == 3


def test_stratified_sample_empty():
    assert _stratified_sample([], n=12) == []


# --- claim judging ---------------------------------------------------------


def _stub_claims_llm(system: str, user: str) -> str:
    # Return judgements for whatever claims were sent (parse ids out of the user text)
    import re

    ids = re.findall(r"### (CLM-\d+)", user)
    return json.dumps(
        {
            "judgements": [
                {
                    "claim_id": cid,
                    "faithfulness": 0.9,
                    "specificity": 0.8,
                    "hallucination": False,
                    "note": f"evidence supports {cid}",
                }
                for cid in ids
            ]
        }
    )


def test_judge_claims_parses_scores():
    judgements = judge_claims(_claims(), call_llm=_stub_claims_llm)
    assert len(judgements) == 4  # sample size
    assert all(j.faithfulness == 0.9 for j in judgements)
    assert all(not j.hallucination for j in judgements)
    assert all(j.claim_id.startswith("CLM-") for j in judgements)


def test_judge_claims_parses_fenced_json():
    def fenced(system: str, user: str) -> str:
        return "```json\n" + _stub_claims_llm(system, user) + "\n```"

    judgements = judge_claims(_claims(), call_llm=fenced)
    assert len(judgements) == 4


def test_judge_claims_empty_ledger():
    assert judge_claims([], call_llm=_stub_claims_llm) == []


# --- brief judging ---------------------------------------------------------


def _stub_brief_llm(system: str, user: str) -> str:
    return json.dumps(
        {
            "rippling_relevance": 0.8,
            "recency_realness": 0.7,
            "usefulness": 0.9,
            "rationale": "angles are specific and campaign-ready",
        }
    )


def test_judge_brief_parses_rubric():
    bj = judge_brief("# Gusto brief\n\nSome content.", call_llm=_stub_brief_llm)
    assert bj.rippling_relevance == 0.8
    assert bj.recency_realness == 0.7
    assert bj.usefulness == 0.9
    assert "campaign-ready" in bj.rationale


# --- end-to-end with file writing ------------------------------------------


def test_judge_competitor_writes_json(tmp_path: Path, monkeypatch) -> None:
    # write a ledger + brief into a tmp outputs dir
    slug = "gusto.com"
    (tmp_path / f"{slug}_intel.json").write_text(
        json.dumps([c.model_dump(mode="json") for c in _claims()]), encoding="utf-8"
    )
    (tmp_path / f"{slug}_brief.md").write_text("# Gusto brief\n\ncontent", encoding="utf-8")

    # force a provider so the skip-no-key path doesn't fire
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("evals.judge.select_provider", lambda: ("openai", "gpt-4o-mini", "k"))

    def stub(system: str, user: str) -> str:
        if "TASK: claims" in user:
            return _stub_claims_llm(system, user)
        return _stub_brief_llm(system, user)

    report = judge_competitor("gusto.com", call_llm=stub, outputs_dir=tmp_path)
    assert report.status == "ok"
    assert report.provider == "openai"
    assert report.brief_judgement is not None
    assert report.aggregate["n"] == 4
    assert report.aggregate["hallucination_rate"] == 0.0
    written = json.loads((tmp_path / f"{slug}_judge.json").read_text(encoding="utf-8"))
    assert written["status"] == "ok"
    assert len(written["claim_judgements"]) == 4


def test_judge_competitor_no_ledger_skips(tmp_path: Path) -> None:
    report = judge_competitor("nope.com", call_llm=lambda s, u: "", outputs_dir=tmp_path)
    assert report.status == "skipped"
    assert "no ledger" in report.note


def test_judge_competitor_no_key_skips(tmp_path: Path, monkeypatch) -> None:
    slug = "gusto.com"
    (tmp_path / f"{slug}_intel.json").write_text(
        json.dumps([c.model_dump(mode="json") for c in _claims()]), encoding="utf-8"
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("evals.judge.select_provider", lambda: None)
    report = judge_competitor("gusto.com", call_llm=lambda s, u: "", outputs_dir=tmp_path)
    assert report.status == "skipped"
    assert "no judge key" in report.note


def test_judge_competitor_call_failure_is_data(tmp_path: Path, monkeypatch) -> None:
    slug = "gusto.com"
    (tmp_path / f"{slug}_intel.json").write_text(
        json.dumps([c.model_dump(mode="json") for c in _claims()]), encoding="utf-8"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("evals.judge.select_provider", lambda: ("openai", "gpt-4o-mini", "k"))

    def boom(system: str, user: str) -> str:
        raise RuntimeError("provider down")

    report = judge_competitor("gusto.com", call_llm=boom, outputs_dir=tmp_path)
    assert report.status == "error"
    assert "provider down" in report.note


def test_report_one_line_ok():
    from evals.judge import ClaimJudgement

    r = JudgeReport(
        competitor="gusto.com",
        provider="openai",
        model="gpt-4o-mini",
        prompt_version="judge_v1",
        judged_at="2026-07-15T00:00:00Z",  # type: ignore[arg-type]
        status="ok",
        claim_judgements=[
            ClaimJudgement(
                claim_id="CLM-1",
                statement="s",
                faithfulness=0.9,
                specificity=0.8,
                hallucination=False,
            )
        ],
        aggregate={"mean_faithfulness": 0.9, "mean_specificity": 0.8, "hallucination_rate": 0.0},
    )
    line = r.one_line()
    assert "openai/gpt-4o-mini" in line
    assert "faithfulness 0.90" in line

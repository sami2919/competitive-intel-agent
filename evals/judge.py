"""Layer-2 cross-family LLM judge (CLAUDE.md §7).

A DIFFERENT model family (GPT-4o-mini or Gemini 2.5 Flash) judges the Claude-produced
ledger + brief — different family so faithfulness is not judged by the model that wrote
the claims (no self-preference bias). Uses direct API calls (not Batch API;
Batch is the production note in DECISIONS §15). Raw httpx, no per-vendor SDK (DECISIONS §8
seam).

Two passes:
  judge_claims  — stratified sample of claims -> faithfulness / specificity / hallucination
  judge_brief   — the brief -> Rippling-relevance / recency-realness / usefulness rubric

Writes outputs/{slug}_judge.json. Skips cleanly (typed status, no crash) when no judge key
is set. The HTTP call is behind a `call_llm` callable so tests stub it without httpx.

Run:  make eval-judge COMPETITOR=gusto.com   # or: python -m evals.judge gusto.com
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from agent.llm import load_prompt
from ledger.models import Claim

JUDGE_PROMPT_VERSION = "judge_v1"
OUTPUTS = Path(__file__).parents[1] / "outputs"

# A call_llm(system, user) -> raw model text. Default does the live httpx dispatch;
# tests pass a stub.
CallLLM = Callable[[str, str], str]

_OPENAI_MODEL = "gpt-4o-mini"
_GEMINI_MODEL = "gemini-3.5-flash"  # 2.5-flash is deprecated for new keys (404s as of 2026-07)


# --- report schema ---------------------------------------------------------


class ClaimJudgement(BaseModel):
    claim_id: str
    statement: str
    faithfulness: float
    specificity: float
    hallucination: bool
    note: str = ""


class BriefJudgement(BaseModel):
    rippling_relevance: float
    recency_realness: float
    usefulness: float
    rationale: str = ""


class JudgeReport(BaseModel):
    competitor: str
    provider: str
    model: str
    prompt_version: str
    judged_at: datetime
    status: str  # "ok" | "skipped" | "error"
    note: str = ""
    claim_judgements: list[ClaimJudgement] = Field(default_factory=list)
    brief_judgement: BriefJudgement | None = None
    aggregate: dict[str, Any] = Field(default_factory=dict)

    def one_line(self) -> str:
        if self.status != "ok":
            return f"judge: {self.status} — {self.note}"
        agg = self.aggregate
        return (
            f"judge: {self.provider}/{self.model} · {len(self.claim_judgements)} claims · "
            f"faithfulness {agg.get('mean_faithfulness', 0):.2f} · "
            f"specificity {agg.get('mean_specificity', 0):.2f} · "
            f"hallucination {agg.get('hallucination_rate', 0) * 100:.0f}%"
            + (
                f" · brief rubric {self.brief_judgement.usefulness:.2f}"
                if self.brief_judgement
                else ""
            )
        )


# --- provider selection ----------------------------------------------------


def _looks_like_real_key(value: str) -> bool:
    """Filter out .env.example placeholders (e.g. 'sk-...') and empty/junk values.

    A real key is non-empty, has no '...' placeholder marker, and is at least 20 chars
    (OpenAI sk- keys are ~50; Gemini keys are ~40). Catches the footgun where a copied
    .env.example placeholder is non-empty so naive presence-checks pick a dead provider
    and 401 — the lessons.md L2 'key is set ≠ key is valid' rule.
    """
    v = value.strip()
    return bool(v) and "..." not in v and len(v) >= 20


def select_provider() -> tuple[str, str, str] | None:
    """Return (provider, model, api_key) — OpenAI preferred, Gemini fallback, else None.

    Skips placeholder values (see _looks_like_real_key) so a copied .env.example with
    'OPENAI_API_KEY=sk-...' doesn't shadow a real GEMINI_API_KEY.
    """
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if _looks_like_real_key(openai_key):
        return ("openai", _OPENAI_MODEL, openai_key.strip())
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if _looks_like_real_key(gemini_key):
        return ("gemini", _GEMINI_MODEL, gemini_key.strip())
    return None


# --- live call_llm (raw httpx, no SDK) -------------------------------------


def _live_call_llm(system: str, user: str) -> str:
    """Synchronous live dispatch to OpenAI or Gemini. Raises on non-200 after one retry."""
    prov = select_provider()
    if prov is None:
        raise RuntimeError("no judge key (OPENAI_API_KEY or GEMINI_API_KEY)")
    provider, model, key = prov
    if provider == "openai":
        return _openai_call(key, model, system, user)
    return _gemini_call(key, model, system, user)


def _with_retry(fn: Callable[[], str]) -> str:
    last: Exception | None = None
    for attempt in range(2):  # one retry + backoff (DECISIONS §12 seam shape)
        try:
            return fn()
        except httpx.HTTPError as exc:
            last = exc
            if attempt == 0:
                time.sleep(0.5)
    # Sanitize: never echo the raw exception (an httpx HTTPStatusError embeds the request
    # URL, which could carry a secret if a caller ever put one in a query param). Report
    # status + a short body hint instead.
    status = getattr(getattr(last, "response", None), "status_code", "?")
    body = ""
    resp = getattr(last, "response", None)
    if resp is not None:
        body = (resp.text or "")[:200].replace("\n", " ")
    raise RuntimeError(f"judge HTTP call failed after retry (status {status}): {body}")


def _openai_call(key: str, model: str, system: str, user: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    def go() -> str:
        r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=60.0)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    return _with_retry(go)


def _gemini_call(key: str, model: str, system: str, user: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }
    # Auth via header, NOT ?key= query param — keeps the key out of the request URL so a
    # raised httpx error (which embeds the URL) can never leak the key into logs/terminal.
    headers = {"x-goog-api-key": key}

    def go() -> str:
        r = httpx.post(url, json=body, headers=headers, timeout=60.0)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    return _with_retry(go)


# --- JSON parsing (tolerant of code fences) --------------------------------


def _parse_json(text: str) -> Any:
    """Parse a JSON object from model text, stripping markdown fences if present."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.DOTALL)
    if fence:
        t = fence.group(1)
    else:
        # grab the outermost {...} block
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            t = m.group(0)
    return json.loads(t)


# --- stratified sampling ---------------------------------------------------


def _stratified_sample(claims: list[Claim], n: int = 12) -> list[Claim]:
    """Sample across confidence tiers + categories so the judge sees the spread, not
    just the high-confidence cluster. Deterministic (no random): tier-round-robin."""
    tiers = [(0.9, ">=0.9"), (0.7, "0.7"), (0.5, "0.5"), (0.0, "<0.5")]
    buckets: dict[str, list[Claim]] = {label: [] for _, label in tiers}
    for c in claims:
        for thresh, label in tiers:
            if round(c.confidence, 2) >= thresh:
                buckets[label].append(c)
                break
    # round-robin one claim per tier, cycling, until n or all exhausted
    picked: list[Claim] = []
    seen: set[str] = set()
    labels = [label for _, label in tiers]
    round_idx = 0
    while len(picked) < n:
        progressed = False
        for label in labels:
            pool = [c for c in buckets[label] if c.id not in seen]
            if round_idx < len(pool):
                picked.append(pool[round_idx])
                seen.add(pool[round_idx].id)
                progressed = True
                if len(picked) >= n:
                    break
        if not progressed:
            break
        round_idx += 1
    return picked


# --- judging ---------------------------------------------------------------


def judge_claims(claims: list[Claim], call_llm: CallLLM) -> list[ClaimJudgement]:
    """Judge a stratified sample; return per-claim scores."""
    sample = _stratified_sample(claims)
    if not sample:
        return []
    system = load_prompt(JUDGE_PROMPT_VERSION)
    lines = ["TASK: claims", "Judge each claim. Return the JSON schema exactly.\n"]
    for c in sample:
        ev = "; ".join(f"[{e.source_url}] {e.excerpt[:240]}" for e in c.evidence[:3])
        lines.append(
            f"### {c.id} (confidence {c.confidence})\nstatement: {c.statement}\nevidence: {ev}"
        )
    raw = call_llm(system, "\n".join(lines))
    data = _parse_json(raw)
    by_id = {c.id: c for c in sample}
    fallback = sample[0] if sample else claims[0]
    out: list[ClaimJudgement] = []
    for j in data.get("judgements", []):
        cid = j.get("claim_id", "")
        out.append(
            ClaimJudgement(
                claim_id=cid,
                statement=by_id.get(cid, fallback).statement,
                faithfulness=float(j.get("faithfulness", 0.0)),
                specificity=float(j.get("specificity", 0.0)),
                hallucination=bool(j.get("hallucination", False)),
                note=str(j.get("note", "")),
            )
        )
    return out


def judge_brief(brief_text: str, call_llm: CallLLM) -> BriefJudgement:
    """Judge the brief on the three-axis rubric."""
    system = load_prompt(JUDGE_PROMPT_VERSION)
    user = f"TASK: brief\n\nJudge this competitor brief:\n\n{brief_text}"
    raw = call_llm(system, user)
    data = _parse_json(raw)
    return BriefJudgement(
        rippling_relevance=float(data.get("rippling_relevance", 0.0)),
        recency_realness=float(data.get("recency_realness", 0.0)),
        usefulness=float(data.get("usefulness", 0.0)),
        rationale=str(data.get("rationale", "")),
    )


def _aggregate(judgements: list[ClaimJudgement]) -> dict[str, Any]:
    if not judgements:
        return {}
    f = [j.faithfulness for j in judgements]
    s = [j.specificity for j in judgements]
    hall = sum(1 for j in judgements if j.hallucination)
    return {
        "n": len(judgements),
        "mean_faithfulness": sum(f) / len(f),
        "mean_specificity": sum(s) / len(s),
        "hallucination_rate": hall / len(judgements),
    }


def _slug(competitor: str) -> str:
    return competitor.replace("/", "_").replace(":", "_")


def judge_competitor(
    competitor: str, call_llm: CallLLM | None = None, outputs_dir: Path | None = None
) -> JudgeReport:
    """Load a competitor's ledger + brief, judge both, write outputs/{slug}_judge.json."""
    out = outputs_dir or OUTPUTS
    slug = _slug(competitor)
    ledger_path = out / f"{slug}_intel.json"
    brief_path = out / f"{slug}_brief.md"

    if not ledger_path.exists():
        return JudgeReport(
            competitor=competitor,
            provider="",
            model="",
            prompt_version=JUDGE_PROMPT_VERSION,
            judged_at=datetime.now(UTC),
            status="skipped",
            note=f"no ledger at {ledger_path} — run `make run COMPETITOR={slug}` first",
        )

    claims = [Claim.model_validate(c) for c in json.loads(ledger_path.read_text(encoding="utf-8"))]
    brief_text = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""

    prov = select_provider()
    if prov is None:
        return JudgeReport(
            competitor=competitor,
            provider="",
            model="",
            prompt_version=JUDGE_PROMPT_VERSION,
            judged_at=datetime.now(UTC),
            status="skipped",
            note="no judge key — set OPENAI_API_KEY or GEMINI_API_KEY to run Layer-2",
        )
    provider, model, _ = prov
    call = call_llm or _live_call_llm

    try:
        claim_j = judge_claims(claims, call)
        brief_j = judge_brief(brief_text, call) if brief_text else None
    except Exception as exc:  # never crash the run — judge failure is data
        return JudgeReport(
            competitor=competitor,
            provider=provider,
            model=model,
            prompt_version=JUDGE_PROMPT_VERSION,
            judged_at=datetime.now(UTC),
            status="error",
            note=f"judge call failed: {exc}",
        )

    report = JudgeReport(
        competitor=competitor,
        provider=provider,
        model=model,
        prompt_version=JUDGE_PROMPT_VERSION,
        judged_at=datetime.now(UTC),
        status="ok",
        claim_judgements=claim_j,
        brief_judgement=brief_j,
        aggregate=_aggregate(claim_j),
    )
    (out / f"{slug}_judge.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report


# --- CLI -------------------------------------------------------------------


def main(argv: list[str]) -> int:
    from dotenv import load_dotenv

    load_dotenv(override=False)
    competitor = (argv[1] if len(argv) > 1 else os.environ.get("COMPETITOR", "")).strip()
    if not competitor:
        print("usage: python -m evals.judge <competitor-domain>   (e.g. gusto.com)")
        return 2
    report = judge_competitor(competitor)
    print(report.one_line())
    if report.status == "ok":
        print(f"  wrote outputs/{_slug(competitor)}_judge.json")
    return 0 if report.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

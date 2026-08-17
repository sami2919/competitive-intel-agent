"""CRO agent entry point — the artifact a reviewer reads to understand the run.

One function, top to bottom: snapshot the page, rank hypotheses off the competitor
ledger, cap generation to what the page's traffic can actually test, generate, gate,
score, render. The model is called exactly once per hypothesis; everything before and
after it is deterministic Python.

Ordering is deliberate. max_runnable_variants() runs BEFORE any model call, so a page
whose traffic cannot support a test costs zero tokens. That is the DRY(E) instinct
applied to feasibility: do the cheap deterministic check first, and never pay for
output nobody can act on.

run_cro() never raises. Every failure — a dead Firecrawl scrape, a missing ledger,
an untestable page — is returned on CroRunResult.error for the caller to report.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent.cost import CostAccumulator
from agent.llm import load_prompt
from cro.compliance import check_compliance, load_facts
from cro.generate import GENERATOR_MODEL, GENERATOR_PROMPT_VERSION, generate_variants
from cro.hypotheses import build_hypotheses, load_canonical_claims
from cro.models import Hypothesis, ScoredVariant
from cro.render import render_json, render_markdown, render_optimizely, write_outputs
from cro.scoring import score_variant
from cro.snapshot import PageSnapshotTool, parse_snapshot
from cro.testplan import TestPlan, build_test_plan, max_runnable_variants
from ledger.models import ToolFailure
from tools._transport import Transport


class CroRunResult(BaseModel):
    """Everything one run produced. `error` non-empty means nothing was written."""

    model_config = {"frozen": True}

    page_url: str
    hypotheses: list[Hypothesis] = []
    scored: list[ScoredVariant] = []
    plan: TestPlan | None = None
    paths: dict[str, str] = {}
    cost_line: str = ""
    error: str = ""


def page_slug(page_url: str) -> str:
    """Filesystem-safe slug from a page URL: /payroll -> rippling-payroll."""
    trimmed = page_url.split("://")[-1].rstrip("/")
    parts = [p for p in trimmed.split("/") if p]
    host = parts[0].removeprefix("www.").split(".")[0]
    tail = parts[-1] if len(parts) > 1 else "home"
    return f"{host}-{tail}"


async def run_cro(
    page_url: str,
    competitor_slug: str,
    transport: Transport,
    client: Any,
    baseline_cvr: float = 0.03,
    weekly_sessions: int = 100_000,
    max_hypotheses: int = 3,
    facts_path: str | Path = "data/rippling_facts.yaml",
    ledger_dir: str | Path = "outputs",
    out_dir: str | Path = "outputs/cro",
) -> CroRunResult:
    """Snapshot -> hypotheses -> generate -> gate -> score -> render. Never raises."""
    started = time.monotonic()
    cost = CostAccumulator()

    ledger_path = Path(ledger_dir) / f"{competitor_slug}_canonical.json"
    if not ledger_path.exists():
        return CroRunResult(
            page_url=page_url,
            error=f"no canonical ledger at {ledger_path} — run the intel agent first",
        )

    claims = load_canonical_claims(ledger_path)
    hypotheses = build_hypotheses(claims, limit=max_hypotheses)
    ledger_ids = {c["id"]: c["canonical_statement"] for c in claims}
    facts = load_facts(facts_path)
    allowed = {**ledger_ids, **{fid: f.statement for fid, f in facts.items()}}

    budget = max_runnable_variants(baseline_cvr, weekly_sessions)
    if budget < 1:
        return CroRunResult(
            page_url=page_url,
            hypotheses=hypotheses,
            error=(
                f"page traffic ({weekly_sessions:,}/wk at {baseline_cvr:.2%}) "
                f"cannot support even one variant inside the 28-day budget — "
                f"no variants generated, no tokens spent"
            ),
        )

    fetched = await PageSnapshotTool(transport).run(url=page_url)
    if isinstance(fetched, ToolFailure):
        return CroRunResult(page_url=page_url, hypotheses=hypotheses, error=fetched.reason)
    try:
        snapshot = parse_snapshot(fetched.url, fetched.raw_excerpt, fetched.fetched_at)
    except ValueError as exc:
        return CroRunResult(page_url=page_url, hypotheses=hypotheses, error=str(exc))

    prompt = load_prompt(GENERATOR_PROMPT_VERSION)
    per_hypothesis = max(1, budget // max(1, len(hypotheses)))

    scored: list[ScoredVariant] = []
    next_index = 1  # VAR ids must stay unique ACROSS hypotheses, not just within one
    for hypothesis in hypotheses:
        variants, usage = generate_variants(
            snapshot, hypothesis, allowed, per_hypothesis, client, prompt, start_index=next_index
        )
        next_index += len(variants)
        cost = cost.add(GENERATOR_MODEL, usage)
        canonical = ledger_ids.get(hypothesis.counters_canonical_id, hypothesis.statement)
        for variant in variants:
            report = check_compliance(variant, ledger_ids, facts)
            scored.append(
                score_variant(variant, hypothesis, canonical, unsourced_claims=report.unsourced)
            )

    shipped = [s for s in scored if s.shippable]
    plan = build_test_plan(max(1, len(shipped)), baseline_cvr, weekly_sessions)
    paths = write_outputs(
        page_slug(page_url),
        render_markdown(snapshot, hypotheses, scored, plan),
        render_json(snapshot, hypotheses, scored, plan),
        render_optimizely(snapshot, shipped),
        out_dir=out_dir,
    )
    return CroRunResult(
        page_url=page_url,
        hypotheses=hypotheses,
        scored=scored,
        plan=plan,
        paths={k: str(v) for k, v in paths.items()},
        cost_line=cost.format_line(time.monotonic() - started, tool_calls=1),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cro",
        description="Generate gated landing-page variants from a competitor ledger.",
    )
    parser.add_argument("--page", required=True, help="Full URL of the page to optimize")
    parser.add_argument(
        "--competitor",
        default="gusto.com",
        help="Competitor slug whose canonical ledger seeds the hypotheses",
    )
    parser.add_argument("--sessions", type=int, default=100_000, help="Weekly sessions on the page")
    parser.add_argument("--cvr", type=float, default=0.03, help="Baseline conversion rate")
    parser.add_argument("--hypotheses", type=int, default=3, help="Max hypotheses to test this run")
    return parser.parse_args(argv)


def check_keys() -> list[str]:
    """Mode-aware required-key check, driven by the tool's declared required_env."""
    from agent.config import missing_keys

    return missing_keys(PageSnapshotTool.required_env)


def main(argv: list[str] | None = None, load_dotenv_first: bool = True) -> int:
    """CLI entry. Returns a process exit code; validates keys before any paid call.

    `load_dotenv_first` exists so tests can exercise the key-validation path. Without
    it, load_env() repopulates os.environ from .env and a test that deleted the keys
    sails through into a REAL paid run — which is exactly what happened while building
    this (70s, real Firecrawl + Sonnet calls, ~$0.07) before the seam was added.
    """
    import os

    from agent.config import load_env, mode
    from agent.llm import make_client
    from tools._transport import LiveTransport

    if load_dotenv_first:
        load_env()
    args = parse_args(argv)

    missing = check_keys()
    if missing:
        print(f"Missing required env for INTEL_MODE={mode()}: {', '.join(missing)}")
        print("Add them to .env (see .env.example), then re-run.")
        return 1

    transport = LiveTransport()
    client = make_client(os.environ["ANTHROPIC_API_KEY"])
    result = asyncio.run(
        run_cro(
            args.page,
            args.competitor,
            transport=transport,
            client=client,
            baseline_cvr=args.cvr,
            weekly_sessions=args.sessions,
            max_hypotheses=args.hypotheses,
        )
    )

    if result.error:
        print(f"CRO run failed: {result.error}")
        return 1

    shipped = [s for s in result.scored if s.shippable]
    print(f"Shipped {len(shipped)} / {len(result.scored)} variants")
    for kind, path in result.paths.items():
        print(f"  {kind}: {path}")
    print(result.cost_line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

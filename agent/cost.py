"""Cost & latency accounting — the run-end cost line (CLAUDE.md §8, DRY(E) discipline).

Pricing is per 1M tokens. Sonnet-5: $2/$10 introductory through 2026-08-31, then
$3/$15 (verified 2026-07-15, platform.claude.com/docs pricing page); we book the
introductory rate and note the step-up date below. Haiku-4-5 unchanged.

Cache hit rate = cache_read / (input + cache_read + cache_creation) — the fraction of
input tokens served from the prompt cache. Log it in the cost line so the DRY(E)
caching strategy is visible per run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Per 1M tokens, USD. Update after the D4 smoke-test verification.
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    # Introductory rate through 2026-08-31; steps up to 3.0/15.0 (cache 0.3/3.75) after.
    "claude-sonnet-5": {
        "input": 2.0,
        "output": 10.0,
        "cache_read": 0.2,
        "cache_write": 2.5,
    },
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
}

MODEL_LABEL = {
    "claude-sonnet-4-6": "Sonnet",
    "claude-sonnet-5": "Sonnet",
    "claude-haiku-4-5": "Haiku",
}


@dataclass(frozen=True)
class Usage:
    """Token usage from one API response (mirrors the Anthropic SDK shape)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @classmethod
    def from_sdk(cls, u: Any) -> Usage:
        return cls(
            input_tokens=getattr(u, "input_tokens", 0),
            output_tokens=getattr(u, "output_tokens", 0),
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0),
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0),
        )


@dataclass(frozen=True)
class CostAccumulator:
    """Immutable per-run cost collector. `add` returns a new accumulator."""

    # per-model token totals: {model: {"input":int, "output":int, "cache_read":int, "cache_creation":int}}
    totals: dict[str, dict[str, int]] = field(default_factory=dict)
    api_spend: float = 0.0  # data-API spend (Firecrawl/ScrapeCreators/etc.) — tracked separately

    def add(self, model: str, usage: Usage) -> CostAccumulator:
        cur = dict(self.totals)
        m = dict(cur.get(model, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}))
        m["input"] += usage.input_tokens
        m["output"] += usage.output_tokens
        m["cache_read"] += usage.cache_read_input_tokens
        m["cache_creation"] += usage.cache_creation_input_tokens
        cur[model] = m
        return CostAccumulator(totals=cur, api_spend=self.api_spend)

    def minus(self, baseline: CostAccumulator) -> CostAccumulator:
        """Per-turn delta: this accumulator's tokens minus a `baseline` snapshot.

        The REPL snapshots session.cost at command entry and prints the delta, so a
        follow-up's cost line reports that turn's spend — not the cumulative session
        total masquerading as a per-run figure (failure_log F16). Floors at 0 per
        counter; api_spend subtracts the same way. Immutable — returns a new object.
        """
        totals: dict[str, dict[str, int]] = {}
        for model, tokens in self.totals.items():
            base = baseline.totals.get(
                model, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
            )
            totals[model] = {k: max(0, v - base.get(k, 0)) for k, v in tokens.items()}
        return CostAccumulator(
            totals=totals, api_spend=max(0.0, self.api_spend - baseline.api_spend)
        )

    def _model_cost(self, model: str, tokens: dict[str, int]) -> float:
        p = PRICING.get(model, PRICING["claude-sonnet-4-6"])
        return (
            tokens["input"] / 1_000_000 * p["input"]
            + tokens["output"] / 1_000_000 * p["output"]
            + tokens["cache_read"] / 1_000_000 * p["cache_read"]
            + tokens["cache_creation"] / 1_000_000 * p["cache_write"]
        )

    def model_cost(self, model: str) -> float:
        return self._model_cost(
            model,
            self.totals.get(model, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}),
        )

    @property
    def llm_cost(self) -> float:
        return sum(self._model_cost(m, t) for m, t in self.totals.items())

    @property
    def total(self) -> float:
        return self.llm_cost + self.api_spend

    @property
    def cache_hit_rate(self) -> float:
        total_in = sum(
            t["input"] + t["cache_read"] + t["cache_creation"] for t in self.totals.values()
        )
        cache_read = sum(t["cache_read"] for t in self.totals.values())
        return cache_read / total_in if total_in else 0.0

    def format_line(self, duration_s: float, tool_calls: int) -> str:
        sonnet = sum(self.model_cost(m) for m in self.totals if "sonnet" in m)
        haiku = sum(self.model_cost(m) for m in self.totals if "haiku" in m)
        # api_spend is only nonzero when a paid fallback (Apify/SerpApi) actually fires and
        # accrues; $0 means the data APIs ran in free tier (not that spend was measured and
        # happened to be zero). Tag it so the line never implies false-precision accounting.
        apis = f"APIs: ${self.api_spend:.2f}" if self.api_spend > 0 else "APIs: $0.00 (free tier)"
        return (
            f"Run complete: {duration_s:.0f}s · {tool_calls} tool calls · "
            f"${self.total:.2f} (cache hit rate {self.cache_hit_rate * 100:.0f}%)\n"
            f"  Sonnet: ${sonnet:.2f} · Haiku: ${haiku:.2f} · {apis}"
        )

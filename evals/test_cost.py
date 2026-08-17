"""Cost accounting unit tests — tokens -> $ per model, cache hit rate, line format."""

from __future__ import annotations

from agent.cost import CostAccumulator, Usage


def test_sonnet_cost_math():
    acc = CostAccumulator().add(
        "claude-sonnet-4-6", Usage(input_tokens=1_000_000, output_tokens=500_000)
    )
    # 1M input * $3 + 500k output * $15 = 3.0 + 7.5 = 10.5
    assert acc.model_cost("claude-sonnet-4-6") == 10.5
    assert acc.llm_cost == 10.5


def test_haiku_cheaper_than_sonnet():
    acc = CostAccumulator().add(
        "claude-haiku-4-5", Usage(input_tokens=1_000_000, output_tokens=500_000)
    )
    # 1M * $1 + 500k * $5 = 1.0 + 2.5 = 3.5
    assert acc.model_cost("claude-haiku-4-5") == 3.5


def test_cache_hit_rate():
    acc = CostAccumulator().add(
        "claude-sonnet-4-6",
        Usage(input_tokens=1000, cache_read_input_tokens=3000, cache_creation_input_tokens=1000),
    )
    # cache_read / (input + cache_read + cache_creation) = 3000 / 5000 = 0.6
    assert abs(acc.cache_hit_rate - 0.6) < 1e-9


def test_immutable_add():
    a = CostAccumulator().add("claude-sonnet-4-6", Usage(input_tokens=100))
    b = a.add("claude-sonnet-4-6", Usage(input_tokens=100))
    assert a.model_cost("claude-sonnet-4-6") != b.model_cost("claude-sonnet-4-6")
    assert a is not b


def test_format_line():
    acc = CostAccumulator().add(
        "claude-sonnet-4-6", Usage(input_tokens=100_000, output_tokens=50_000)
    )
    line = acc.format_line(duration_s=252.0, tool_calls=31)
    assert "Run complete" in line
    assert "31 tool calls" in line
    assert "Sonnet:" in line
    assert "cache hit rate" in line


def test_format_line_tags_zero_api_spend_as_free_tier():
    """$0 api_spend means free tier, not measured-and-zero — tag it so the line is honest."""
    acc = CostAccumulator().add("claude-sonnet-4-6", Usage(input_tokens=100, output_tokens=50))
    line = acc.format_line(duration_s=10.0, tool_calls=2)
    assert "APIs: $0.00 (free tier)" in line


def test_format_line_shows_accrued_api_spend_without_tag():
    """When a paid fallback accrues real spend, show the number without the free-tier tag."""
    acc = CostAccumulator(api_spend=0.02).add(
        "claude-sonnet-4-6", Usage(input_tokens=100, output_tokens=50)
    )
    line = acc.format_line(duration_s=10.0, tool_calls=2)
    assert "APIs: $0.02" in line
    assert "free tier" not in line


def test_minus_gives_per_turn_delta():
    from agent.cost import CostAccumulator, Usage

    c0 = CostAccumulator().add("claude-sonnet-5", Usage(input_tokens=1000, output_tokens=500))
    c1 = c0.add("claude-sonnet-5", Usage(input_tokens=200, output_tokens=100)).add(
        "claude-haiku-4-5", Usage(input_tokens=50, output_tokens=25)
    )
    delta = c1.minus(c0)

    assert delta.totals["claude-sonnet-5"]["input"] == 200
    assert delta.totals["claude-sonnet-5"]["output"] == 100
    assert delta.totals["claude-haiku-4-5"]["input"] == 50
    # baseline untouched (immutability) and total delta = cost difference
    assert c0.totals["claude-sonnet-5"]["input"] == 1000
    assert abs(delta.total - (c1.total - c0.total)) < 1e-9


def test_minus_never_negative():
    from agent.cost import CostAccumulator, Usage

    c0 = CostAccumulator().add("claude-sonnet-5", Usage(input_tokens=100))
    delta = CostAccumulator().minus(c0)
    assert delta.totals.get("claude-sonnet-5", {"input": 0})["input"] == 0

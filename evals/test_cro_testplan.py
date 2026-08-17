"""Experiment sizing math — hand-checked against the two-proportion z-test formula."""

from __future__ import annotations

import pytest

from cro.testplan import (
    build_test_plan,
    max_runnable_variants,
    required_sample_per_arm,
)


def test_sample_size_matches_hand_calculation():
    # p1=0.03, p2=0.036, z_sum=1.959964+0.841621=2.801585
    # var    = 0.03*0.97 + 0.036*0.964 = 0.0291 + 0.034704 = 0.063804
    # n = 2.801585^2 * 0.063804 / 0.006^2
    #   = 7.8488785 * 0.063804 / 0.000036 = 0.50078984 / 0.000036 = 13910.83 -> 13911
    assert required_sample_per_arm(0.03, 0.20) == 13911


def test_smaller_mde_needs_more_traffic():
    # Halving the detectable effect roughly quadruples the sample — the core
    # intuition a growth team needs before asking for a 5% lift readout.
    coarse = required_sample_per_arm(0.03, 0.20)
    fine = required_sample_per_arm(0.03, 0.10)
    assert fine > coarse * 3.5


def test_higher_power_needs_more_traffic():
    assert required_sample_per_arm(0.03, 0.20, power=0.90) > required_sample_per_arm(
        0.03, 0.20, power=0.80
    )


def test_each_added_arm_lengthens_the_test():
    two = build_test_plan(2, baseline_cvr=0.03, weekly_sessions=40_000)
    four = build_test_plan(4, baseline_cvr=0.03, weekly_sessions=40_000)
    assert four.days_required > two.days_required
    assert four.n_arms == 5  # 4 variants + control


def test_low_traffic_page_is_not_runnable():
    plan = build_test_plan(6, baseline_cvr=0.03, weekly_sessions=400)
    assert plan.runnable is False
    assert "NOT runnable" in plan.trace
    assert "cut to fewer arms" in plan.trace


def test_high_traffic_page_is_runnable():
    plan = build_test_plan(2, baseline_cvr=0.05, weekly_sessions=200_000)
    assert plan.runnable is True
    assert plan.days_required <= 28


def test_max_runnable_variants_caps_generation():
    # A thin page supports nothing; a fat one supports several.
    assert max_runnable_variants(baseline_cvr=0.03, weekly_sessions=400) == 0
    assert max_runnable_variants(baseline_cvr=0.05, weekly_sessions=500_000) >= 3


def test_trace_is_human_readable():
    trace = build_test_plan(3, baseline_cvr=0.03, weekly_sessions=100_000).trace
    assert "4 arms" in trace
    assert "3.00%" in trace
    assert "MDE +20% rel" in trace


@pytest.mark.parametrize(
    "kwargs",
    [
        {"baseline_cvr": 0.0, "mde_relative": 0.2},
        {"baseline_cvr": 1.0, "mde_relative": 0.2},
        {"baseline_cvr": 0.03, "mde_relative": 0.0},
        {"baseline_cvr": 0.9, "mde_relative": 0.5},  # target CVR >= 1.0
    ],
)
def test_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        required_sample_per_arm(**kwargs)


def test_unsupported_alpha_raises_rather_than_approximating():
    # A wrong z-value is a wrong ship/no-ship call — fail loudly instead.
    with pytest.raises(ValueError, match="alpha must be one of"):
        required_sample_per_arm(0.03, 0.20, alpha=0.037)

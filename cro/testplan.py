"""Experiment sizing. Pure math, zero LLM, zero dependencies.

This is the check that stops the agent producing a fantasy. An agent that happily
emits eight variants for a page with 400 sessions/week has not designed a test —
it has designed something that will never reach significance, and someone will
read noise as a result four weeks later.

Two-proportion z-test, unpooled variance, two-sided:

    n_per_arm = (z_alpha/2 + z_beta)^2 * (p1(1-p1) + p2(1-p2)) / (p2 - p1)^2

z-values are hardcoded for the two conventional settings so this stays
dependency-free (no scipy). Anything else raises rather than silently
approximating — a wrong z is a wrong ship/no-ship call.
"""

from __future__ import annotations

from pydantic import BaseModel

# z_{1-alpha/2} for two-sided tests, and z_{power}. Conventional settings only.
_Z_ALPHA_TWO_SIDED = {0.05: 1.959964, 0.10: 1.644854, 0.01: 2.575829}
_Z_POWER = {0.80: 0.841621, 0.90: 1.281552}


class TestPlan(BaseModel):
    """Sizing verdict for one experiment. `runnable` is the ship/no-ship signal."""

    model_config = {"frozen": True}

    n_arms: int  # control + variants
    baseline_cvr: float
    mde_relative: float
    required_per_arm: int
    weekly_sessions: int
    days_required: int
    runnable: bool
    trace: str


def required_sample_per_arm(
    baseline_cvr: float,
    mde_relative: float,
    alpha: float = 0.05,
    power: float = 0.80,
) -> int:
    """Sessions needed per arm to detect a `mde_relative` lift on `baseline_cvr`.

    mde_relative is relative, not absolute: 0.20 means "detect a 20% lift", i.e.
    3.0% -> 3.6%. Relative is how growth teams actually talk about targets.
    """
    if not 0.0 < baseline_cvr < 1.0:
        raise ValueError(f"baseline_cvr must be in (0,1), got {baseline_cvr}")
    if mde_relative <= 0.0:
        raise ValueError(f"mde_relative must be > 0, got {mde_relative}")
    if alpha not in _Z_ALPHA_TWO_SIDED:
        raise ValueError(f"alpha must be one of {sorted(_Z_ALPHA_TWO_SIDED)}, got {alpha}")
    if power not in _Z_POWER:
        raise ValueError(f"power must be one of {sorted(_Z_POWER)}, got {power}")

    p1 = baseline_cvr
    p2 = baseline_cvr * (1.0 + mde_relative)
    if p2 >= 1.0:
        raise ValueError(f"mde_relative {mde_relative} pushes target CVR to {p2} >= 1.0")

    z_sum = _Z_ALPHA_TWO_SIDED[alpha] + _Z_POWER[power]
    variance = p1 * (1.0 - p1) + p2 * (1.0 - p2)
    n = (z_sum**2) * variance / ((p2 - p1) ** 2)
    return _ceil_int(n)


def build_test_plan(
    n_variants: int,
    baseline_cvr: float,
    weekly_sessions: int,
    mde_relative: float = 0.20,
    max_days: int = 28,
    alpha: float = 0.05,
    power: float = 0.80,
) -> TestPlan:
    """Size the experiment and decide whether it can finish inside `max_days`.

    Traffic splits evenly across control + variants, so every arm added makes the
    test longer. That trade-off is the whole reason this function exists.
    """
    if n_variants < 1:
        raise ValueError(f"n_variants must be >= 1, got {n_variants}")
    if weekly_sessions < 1:
        raise ValueError(f"weekly_sessions must be >= 1, got {weekly_sessions}")

    n_arms = n_variants + 1  # +1 for control
    per_arm = required_sample_per_arm(baseline_cvr, mde_relative, alpha=alpha, power=power)
    sessions_per_arm_per_week = weekly_sessions / n_arms
    days = _ceil_int(per_arm / sessions_per_arm_per_week * 7.0)
    runnable = days <= max_days

    verdict = (
        f"runnable in {days}d (<= {max_days}d budget)"
        if runnable
        else (
            f"NOT runnable: needs {days}d > {max_days}d budget — "
            f"cut to fewer arms, accept a larger MDE, or send more traffic"
        )
    )
    trace = (
        f"{n_arms} arms (1 control + {n_variants}) · baseline {baseline_cvr:.2%} · "
        f"MDE +{mde_relative:.0%} rel · alpha {alpha} power {power:.0%} → "
        f"{per_arm:,}/arm at {sessions_per_arm_per_week:,.0f} sessions/arm/week → {verdict}"
    )
    return TestPlan(
        n_arms=n_arms,
        baseline_cvr=baseline_cvr,
        mde_relative=mde_relative,
        required_per_arm=per_arm,
        weekly_sessions=weekly_sessions,
        days_required=days,
        runnable=runnable,
        trace=trace,
    )


def max_runnable_variants(
    baseline_cvr: float,
    weekly_sessions: int,
    mde_relative: float = 0.20,
    max_days: int = 28,
) -> int:
    """How many variants this page's traffic can actually support. 0 = none.

    Used to cap generation BEFORE spending tokens — the DRY(E) move: do not
    generate six variants when the traffic supports two.
    """
    for n in range(8, 0, -1):
        if build_test_plan(n, baseline_cvr, weekly_sessions, mde_relative, max_days).runnable:
            return n
    return 0


def _ceil_int(value: float) -> int:
    """Ceiling without importing math — sample sizes always round up."""
    truncated = int(value)
    return truncated if truncated == value else truncated + 1

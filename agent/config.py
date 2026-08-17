"""Startup env loading + mode-aware key validation (CLAUDE.md §14 secrets: fail fast).

INTEL_MODE=live  -> require ANTHROPIC_API_KEY + every instantiated tool's required_env.
INTEL_MODE=demo  -> require only ANTHROPIC_API_KEY (data APIs replay from fixtures).

Tool-declared required_env (tools/_base.py) is the source of truth for which data-API
keys a run needs, so adding a Lane B tool (e.g. news_posts declaring EXA_API_KEY) extends
validation automatically — no hardcoded list here.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

ANTHROPIC_KEY = "ANTHROPIC_API_KEY"


def load_env(env_path: str = ".env") -> None:
    """Load .env into os.environ. Does not override vars already set in the shell."""
    load_dotenv(env_path, override=False)


def mode() -> str:
    """live (default) or demo — set by the Makefile per target."""
    return os.environ.get("INTEL_MODE", "live").lower()


def missing_keys(tool_env: list[str]) -> list[str]:
    """Return the required env vars that are absent/empty for the current mode.

    ANTHROPIC_API_KEY is always required (orchestrator + extractor run live in both
    modes). Data-API keys are required only in live mode — demo replays them.
    """
    required = [ANTHROPIC_KEY]
    if mode() == "live":
        required.extend(tool_env)
    return [k for k in required if not os.environ.get(k, "").strip()]

"""CLI entrypoints — one-shot runs and the interactive REPL (Phase 5).

`python -m agent.repl gusto.com`  -> one-shot: unchanged scripted run (clarifying Q
                                     auto-answered "both"; outputs + re-run diff).
`python -m agent.repl`            -> interactive REPL:
      › analyze Bamboo HR          resolve name -> domain, confirm, fresh session, full run
      › dig deeper on pricing      follow-up on the live thread (answers from the ledger)
      › run again for gusto        fresh session for the named competitor (or current if bare)
      › compare with rippling      compare the current competitor vs Rippling (cites both ledgers)
      › quit / Ctrl-C / Ctrl-D     exit with the cumulative session cost line

Deterministic router (classify_turn) — no LLM decides what a command means. Each turn is
wrapped so one failure reports and returns to the prompt instead of killing the session.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from agent.loop import RunResult, compare_with_rippling, follow_up, run_competitor
from agent.resolve import normalize_domain, resolve_competitor
from agent.session import Session
from ledger.persist import diff_ledgers, load_ledger
from tools._base import BaseTool
from tools._transport import LiveTransport
from tools.crawl_site import CrawlSiteTool
from tools.g2_reviews import G2ReviewsTool
from tools.google_ads import GoogleAdsTool
from tools.jobs_signals import JobsSignalsTool
from tools.linkedin_posts import LinkedInPostsTool
from tools.meta_ads import MetaAdsTool
from tools.news_press import NewsPressTool
from tools.social_posts import SocialPostsTool
from tools.wayback_diff import WaybackDiffTool

console = Console()

# Tools wired for a live run, in call order.
DEFAULT_TOOLS: list[type[BaseTool]] = [
    CrawlSiteTool,
    MetaAdsTool,
    GoogleAdsTool,
    WaybackDiffTool,
    NewsPressTool,
    JobsSignalsTool,
    G2ReviewsTool,
    SocialPostsTool,
    LinkedInPostsTool,
]

_QUIT = {"quit", "exit", "q"}


# ---------------------------------------------------------------------------
# Phase 7 — deterministic intent router (no LLM decides what a command means)
# ---------------------------------------------------------------------------
# UX-tunable pattern table: edit these to match how YOU phrase commands. The fall-through
# is "followup" — anything not recognized as a special command is a ledger-backed
# conversational question on the current session, so the user can ask anything.
@dataclass(frozen=True)
class TurnIntent:
    """The router's decision for one user turn.

    kind: "fresh" (start/re-run a competitor) | "compare" (vs Rippling) | "quit" | "followup".
    target: for "fresh", the name/domain to analyze; None means re-run the current
            competitor ("run again" with no target). None for non-fresh intents.
    """

    kind: str
    target: str | None = None


# Phrases that re-run a competitor (with an optional "for <target>"). A regex, not a
# prefix list, so it also catches natural speech the prefix list missed ("run this again
# for gusto", "run it again", "do it again", "start over for X", "reset") and tolerates
# extra whitespace. Add aliases by extending the alternation. The captured group is the
# text after the phrase (e.g. "for gusto" / "gusto" / "").
_RUN_AGAIN_RE = re.compile(
    r"^(?:"
    r"run again|run-again|again|redo|rerun|re-run|restart|reset"
    r"|run(?:\s+(?:this|it|that))?\s+again"
    r"|do(?:\s+(?:this|it|that))?\s+again"
    r"|start\s+(?:over|fresh|again)"
    r")\b\s*(.*)$",
    re.IGNORECASE,
)


def _target_from_rest(rest: str) -> str | None:
    """Strip an optional leading 'for' from the post-phrase text; None if nothing's left."""
    rest = rest.strip()
    if rest.lower().startswith("for "):
        rest = rest[4:].strip()
    elif rest.lower() == "for":
        rest = ""
    return rest or None


def _match_run_again(text: str) -> tuple[bool, str | None]:
    """Return (matched, target). matched=True if `text` starts with a run-again phrase;
    target is the name/domain after it (and an optional 'for'), or None if none given."""
    m = _RUN_AGAIN_RE.match(text)
    if not m:
        return False, None
    return True, _target_from_rest(m.group(1))


# ---------------------------------------------------------------------------
# Phase 7 follow-on — switching to a NEW competitor mid-session
# ---------------------------------------------------------------------------
# The bug this fixes (from a live run): "Now do the same thing for a different
# company called Workday" — with a Gusto session live — fell through to `followup`
# (no `analyze` prefix, no run-again keyword, no "rippling"). The result was a
# half-scoped conversational answer that pulled a few tools but never clustered,
# grounded, or wrote a brief, and the agent admitted "no confidence scores or
# CAN-xxx clusters assigned." A new-competitor request must route to a fresh
# `analyze` so it gets the full pipeline + a persisted brief.
#
# High-precision by design: a false fresh-run wastes ~$0.50 and a brief, while a
# missed switch just means the user types `analyze X`. So the detector requires an
# UNAMBIGUOUS switch signal — either an explicit "a different/new/another company"
# qualifier or "switch to" — OR a bare domain token as the whole turn. A topic
# follow-up like "do the same thing for their pricing" (no qualifier, no domain)
# stays a follow-up. Add aliases by extending the alternations below.
_NEW_COMPETITOR_RE = re.compile(
    r"^(?:"
    # "now do the same (thing) for/with a different/new/another company (called) X"
    r"(?:now\s+)?do\s+the\s+same(?:\s+thing)?\s+(?:for|with)\s+"
    r"a\s+(?:different|new|another)\s+company(?:\s+called)?\s+(.+)"
    # "switch to X" / "now switch to X"
    r"|(?:now\s+)?switch\s+to\s+(.+)"
    # "now do/analyze/run <domain>" — target is explicitly a domain, never a topic
    r"|(?:now\s+)?(?:do|analyze|run)\s+([a-z0-9-]+\.(?:com|io|co|net|org|ai|app|dev|us|uk|de|fr|eu))\b.*"
    r")$",
    re.IGNORECASE,
)

# A turn that IS just a bare domain (e.g. "workday.com") — a strong switch signal.
_DOMAIN_ONLY_RE = re.compile(
    r"^[a-z0-9-]+\.(?:com|io|co|net|org|ai|app|dev|us|uk|de|fr|eu)$", re.IGNORECASE
)


def _base_slug(text: str) -> str:
    """Comparable competitor identity: 'Gusto' / 'gusto.com' / 'https://Gusto.com' -> 'gusto'."""
    return normalize_domain(text).split(".")[0]


def _is_different(target: str, current: str | None) -> bool:
    """True if `target` names a different competitor than `current` (by base slug)."""
    if current is None:
        return True
    return _base_slug(target) != _base_slug(current)


def _first_nonempty(groups: tuple[str | None, ...]) -> str | None:
    for g in groups:
        if g:
            return g.strip()
    return None


def _match_new_competitor(text: str, current_competitor: str | None) -> str | None:
    """Return a target name/domain if `text` explicitly signals switching to a NEW
    competitor different from `current_competitor`, else None.

    Qualified-switch phrasings that name the SAME competitor (e.g. "do the same for a
    different company called Gusto" while on gusto.com) return None — not a switch.
    """
    t = text.strip()
    m = _NEW_COMPETITOR_RE.match(t)
    if m:
        target = _first_nonempty(m.groups())
        if target and _is_different(target, current_competitor):
            return target
        return None
    if _DOMAIN_ONLY_RE.fullmatch(t) and _is_different(t, current_competitor):
        return t
    return None


# A Rippling mention is comparative only with a comparative signal — else a follow-up that
# merely references Rippling (e.g. "the rippling-relevance section") would mis-route to a
# fresh comparison. With no session, a Rippling mention is treated as compare (nothing to
# follow up on yet). "rippling and <X>" / "<X> and rippling" is a comparative structure too.
_RIPPLING_COMPARE_WORDS: tuple[str, ...] = (
    "compare",
    "compared",
    "comparison",
    "similar",
    "alike",
    "same",
    "versus",
    "differ",
    "different",
    "contrast",
    "match",
    "overlap",
)


def _is_rippling_compare(lowered: str) -> bool:
    """A Rippling mention (caller-guarded) is comparative with a comparative word, a 'vs'/
    'versus' token, or a 'rippling and X' / 'X and rippling' structure."""
    return (
        any(w in lowered for w in _RIPPLING_COMPARE_WORDS)
        or "vs" in lowered.split()
        or "versus" in lowered.split()
        or "rippling and " in lowered
        or " and rippling" in lowered
    )


def classify_turn(
    text: str, has_session: bool, current_competitor: str | None = None
) -> TurnIntent:
    """Deterministic router (Phase 7) — no LLM decides what a command means.

    Precedence: quit > analyze-prefix > run-again > new-competitor-switch >
    compare(Rippling) > no-session(fresh) > follow-up. "analyze rippling.com" and
    "run again for rippling" stay fresh runs (caught before the Rippling heuristic). A
    Rippling mention routes to compare only with a comparative signal (or no session) —
    so "the rippling-relevance section" stays a follow-up. A mid-session switch to a
    different competitor (explicit "different company" phrasing, "switch to", or a bare
    domain) routes to fresh so it gets the full pipeline + a persisted brief instead of
    a half-scoped follow-up. Any other text with a live session is a follow-up.
    """
    lowered = text.lower()
    if lowered in _QUIT:
        return TurnIntent(kind="quit")
    if lowered.startswith("analyze "):
        return TurnIntent(kind="fresh", target=text[len("analyze ") :].strip() or None)
    matched, target = _match_run_again(text)
    if matched:
        return TurnIntent(kind="fresh", target=target)
    new_target = _match_new_competitor(text, current_competitor)
    if new_target is not None:
        return TurnIntent(kind="fresh", target=new_target)
    if "rippling" in lowered and (not has_session or _is_rippling_compare(lowered)):
        return TurnIntent(kind="compare")
    if not has_session:
        return TurnIntent(kind="fresh", target=text)
    return TurnIntent(kind="followup")


def _slug(competitor: str) -> str:
    return competitor.replace("/", "_").replace(":", "_")


def _archive_prior_outputs(out: Path, slug: str) -> None:
    """Move any existing outputs for `slug` to outputs/history/{slug}/{timestamp}/.

    A re-run during a bad-luck window (Wayback 503, zero Meta ads) must never
    destroy the prior good artifacts (failure_log F15). No-op on first run.
    """
    existing = [
        p
        for p in (
            out / f"{slug}_brief.md",
            out / f"{slug}_intel.json",
            out / f"{slug}_canonical.json",
        )
        if p.exists()
    ]
    if not existing:
        return
    stamp = time.strftime("%Y%m%dT%H%M%S")
    archive = out / "history" / slug / stamp
    archive.mkdir(parents=True, exist_ok=True)
    for p in existing:
        p.rename(archive / p.name)
    console.print(f"[dim]  archived prior outputs → {archive}/[/dim]")


def _write_outputs(result: RunResult) -> None:
    """Persist the brief + serialized claims ledger to outputs/ (CLAUDE.md §1)."""
    out = Path("outputs")
    out.mkdir(exist_ok=True)
    slug = _slug(result.competitor)
    _archive_prior_outputs(out, slug)
    (out / f"{slug}_brief.md").write_text(result.brief, encoding="utf-8")
    _write_ledger(result.competitor, result.ledger)
    wrote = f"outputs/{slug}_brief.md · outputs/{slug}_intel.json"
    if result.canonical_claims:
        canon_json = json.dumps(
            [c.model_dump(mode="json") for c in result.canonical_claims], indent=2, default=str
        )
        (out / f"{slug}_canonical.json").write_text(canon_json, encoding="utf-8")
        wrote += f" · outputs/{slug}_canonical.json"
    console.print(f"[green]✓ wrote[/green] {wrote}")


def _write_ledger(competitor: str, ledger: list) -> None:
    """Re-persist just the intel JSON — follow-ups may have grown the ledger."""
    out = Path("outputs")
    out.mkdir(exist_ok=True)
    ledger_json = json.dumps([c.model_dump(mode="json") for c in ledger], indent=2, default=str)
    (out / f"{_slug(competitor)}_intel.json").write_text(ledger_json, encoding="utf-8")


def _is_auth_error(exc: Exception) -> bool:
    """Duck-typed 401/invalid-key detection (decoupled from the anthropic SDK types)."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status == 401 or "401" in text or ("invalid" in text and "api key" in text)


def _fatal_auth() -> None:
    console.print(
        "[red]Anthropic rejected ANTHROPIC_API_KEY (401 invalid).[/red]\n"
        "Re-copy a fresh key from console.anthropic.com into .env "
        "(paste only the key — no trailing comment or spaces)."
    )
    sys.exit(1)


def _validate_keys() -> None:
    """Mode-aware validation: ANTHROPIC always; data-API keys only in live mode."""
    from agent.config import missing_keys, mode

    tool_env = sorted({k for cls in DEFAULT_TOOLS for k in cls.required_env})
    missing = missing_keys(tool_env)
    if missing:
        console.print(
            f"[red]Missing required API keys for {mode()} mode:[/red] {', '.join(missing)}"
        )
        console.print("Set them in .env (see .env.example).")
        sys.exit(1)


def _print_turn_cost(session: Any, cost0: Any, steps0: int, duration_s: float) -> None:
    """Per-TURN cost line + a dim session total.

    A follow-up's 'Run complete' line used to print cumulative session totals
    ('9 tool calls · $0.61' for a 0-tool, ~$0.15 turn) — misleading as a per-run
    figure (failure_log F16). Snapshot at command entry, print the delta.
    """
    turn = session.cost.minus(cost0)
    line = turn.format_line(duration_s=duration_s, tool_calls=session.steps - steps0)
    console.print(line.replace("Run complete:", "Turn complete:", 1))
    console.print(
        f"[dim]  session total: ${session.cost.total:.2f} · {session.steps} tool calls[/dim]"
    )


# --- interactive pieces ------------------------------------------------------


def _ask_user(question: str) -> str:
    """The clarifying-question pause — the agent actually waits for the human."""
    console.print(f"\n[bold yellow]agent asks:[/bold yellow] {question}")
    return console.input("[bold cyan]your answer › [/bold cyan]").strip() or "both"


async def _resolve_and_confirm(target: str, transport: Any, client: Any) -> str | None:
    """Resolve name/domain and confirm with the user before any credits are spent.

    Returns the confirmed domain, or None if the user aborted.
    """
    res = await resolve_competitor(target, transport, client)
    if res.domain is None:
        console.print(f"[yellow]Couldn't resolve '{target}'[/yellow] ({res.note})")
        typed = console.input("Type the domain (e.g. gusto.com), or Enter to cancel › ").strip()
        return normalize_domain(typed) if "." in typed else None

    if res.method == "domain-input":
        return res.domain  # user typed a domain — no confirmation theater needed

    ans = (
        console.input(
            f"Resolved [bold]'{target}'[/bold] → [bold green]{res.domain}[/bold green] "
            f"({res.method}) — proceed? [Y / n / type the domain] › "
        )
        .strip()
        .lower()
    )
    if ans in {"", "y", "yes"}:
        return res.domain
    if "." in ans:
        return normalize_domain(ans)
    return None


async def _analyze(
    domain: str, client: Any, tools: list[BaseTool]
) -> tuple[Session | None, RunResult | None]:
    """Fresh session: full run with a real clarifying-question pause, outputs written."""
    prior = load_ledger(_slug(domain))  # None on first run for this competitor
    console.print(f"[bold]analyze {domain}[/bold]  ({len(tools)} tools)")
    result = await run_competitor(
        domain, client, tools, ask_user=_ask_user, start_monotonic=time.monotonic()
    )
    console.print()
    console.print(result.brief, markup=False)
    _write_outputs(result)
    if prior is not None:
        d = diff_ledgers(prior, result.ledger)
        console.print(f"[cyan]re-run diff vs prior:[/cyan] {d.summary}")
    return result.session, result


async def _repl() -> None:
    """The interactive loop. Deterministic router: analyze | follow-up | quit."""
    from agent.config import mode

    client = _make_client()
    transport = LiveTransport()
    tools = [cls(transport=transport) for cls in DEFAULT_TOOLS]
    session: Session | None = None

    console.print("[bold cyan]Competitive Intel Agent[/bold cyan] — interactive")
    console.print(
        f"[dim]{mode()} mode · {len(tools)} tools · type a competitor name or domain "
        f"(e.g. 'analyze Bamboo HR' or 'gusto.com') · follow-ups run on the live session · "
        f"quit to exit[/dim]"
    )
    try:
        while True:
            try:
                text = console.input("\n[bold cyan]› [/bold cyan]").strip()
            except (KeyboardInterrupt, EOFError):
                break
            if not text:
                continue

            intent = classify_turn(
                text,
                has_session=session is not None,
                current_competitor=session.competitor if session else None,
            )
            try:
                if intent.kind == "quit":
                    break
                elif intent.kind == "fresh":
                    target = intent.target
                    if target is None:
                        if session is None:
                            console.print(
                                "[dim]analyze a competitor first (e.g. 'analyze gusto.com')[/dim]"
                            )
                            continue
                        target = session.competitor  # "run again" with no target -> re-run current
                    domain = await _resolve_and_confirm(target, transport, client)
                    if domain is None:
                        console.print("[dim]cancelled[/dim]")
                        continue
                    session, _result = await _analyze(domain, client, tools)
                elif intent.kind == "compare":
                    if session is None:
                        console.print(
                            "[dim]analyze a competitor first (e.g. 'analyze gusto.com'), "
                            "then ask to compare with Rippling.[/dim]"
                        )
                        continue
                    t0 = time.monotonic()
                    cost0, steps0 = session.cost, session.steps
                    answer = await compare_with_rippling(session, client)
                    console.print()
                    console.print(answer, markup=False)
                    _print_turn_cost(session, cost0, steps0, time.monotonic() - t0)
                else:  # followup — conversational, reuses the live ledger
                    t0 = time.monotonic()
                    cost0, steps0 = session.cost, session.steps
                    answer = await follow_up(session, text, client, tools)
                    console.print()
                    console.print(answer, markup=False)
                    _write_ledger(session.competitor, session.ledger)
                    _print_turn_cost(session, cost0, steps0, time.monotonic() - t0)
            except Exception as exc:  # one bad turn must not kill the session
                if _is_auth_error(exc):
                    _fatal_auth()
                console.print(f"[red]turn failed:[/red] {exc}")
    finally:
        await transport.aclose()

    if session is not None:
        console.print("\n[dim]session total:[/dim]")
        console.print(session.cost.format_line(duration_s=0.0, tool_calls=session.steps))


# --- entrypoints --------------------------------------------------------------


def _make_client() -> Any:
    from agent.llm import make_client

    return make_client(os.environ["ANTHROPIC_API_KEY"])


async def run(competitor: str | None) -> None:
    """CLI entrypoint. None -> interactive REPL. Else one scripted (one-shot) run."""
    from agent.config import load_env, mode

    load_env()
    _validate_keys()

    if competitor is None:
        await _repl()
        return

    client = _make_client()
    transport = LiveTransport()
    tools = [cls(transport=transport) for cls in DEFAULT_TOOLS]
    domain = normalize_domain(competitor)
    prior = load_ledger(_slug(domain))
    console.print(f"[bold]analyze {domain}[/bold]  ({mode()} mode · {len(tools)} tools)")
    t0 = time.monotonic()
    try:
        result = await run_competitor(
            domain, client, tools, clarifying_answer="both", start_monotonic=t0
        )
    except Exception as exc:
        if _is_auth_error(exc):
            _fatal_auth()
        raise
    finally:
        await transport.aclose()
    # (clarifying Q is printed by run_competitor at auto-answer time, in order)
    _write_outputs(result)
    if prior is not None:
        d = diff_ledgers(prior, result.ledger)
        console.print(f"[cyan]re-run diff vs prior:[/cyan] {d.summary}")


def main() -> None:
    competitor = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run(competitor))


if __name__ == "__main__":
    main()

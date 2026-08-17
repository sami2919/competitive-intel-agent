"""Orchestrator loop — the artifact the hiring manager reads first.

Hand-rolled Anthropic tool_use cycle: plan -> act (call a tool) -> observe -> re-plan
-> synthesize. One planning brain, deterministic tools, failures as data.

Eng-review lock-in encoded here:
  D6   - carry claim DIGESTS in the message thread (not raw pages, not the full
         ledger). The full Claim list lives in memory and is loaded at synthesis only.
         Token usage stays flat across turns (DRY(E)).
  D1   - tools are deterministic BaseTool subclasses behind the shared transport.
  D5/D7- extraction is batched (Haiku); ads confidence branch wired in Phase 2.
  Step budget hard cap = STEP_BUDGET; skip-empty (a tool returning empty is noted,
  not retried more than once); exactly ONE clarifying question before tool calls.
  Cost hook (response.usage) accumulates every turn -> the run-end cost line.

Conversational layer (Phase 5): all run state lives in a Session (agent/session.py).
The clarifying question pauses for a real answer via `ask_user` in interactive mode;
`follow_up` re-enters this same cycle on the live message thread, so "dig deeper on
pricing" reads the cached context + ledger digests instead of re-crawling.
CLI entrypoints (one-shot + REPL) live in agent/repl.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.markup import escape

from agent.cost import CostAccumulator, Usage
from agent.digest import digest_preview, ledger_digest, source_coverage_line
from agent.extractor import EXTRACTOR_MODEL, EXTRACTOR_PROMPT_VERSION, extract_claims_sync
from agent.llm import (
    SYNTHESIS_MAX_TOKENS,
    create,
    create_text,
    load_prompt,
    text_of,
    tool_uses_of,
)
from agent.session import Session
from ledger.build import claim_from_raw
from ledger.clustering import CLUSTERING_MODEL, cluster_claims
from ledger.grounding import check_grounding
from ledger.models import CanonicalClaim, Claim, ToolFailure
from ledger.persist import load_canonical_claims, load_ledger
from ledger.summarize import (
    format_confidence_by_source,
    format_evidence_quality,
    format_how_to_read,
)
from tools._base import BaseTool

console = Console()

ORCHESTRATOR_MODEL = "claude-sonnet-5"  # D4: fallback claude-sonnet-4-6 if unavailable
ORCHESTRATOR_PROMPT_VERSION = "orchestrator_v4"
STEP_BUDGET = 35

# Phase 7 — conversational follow-ups + comparative mode.
# follow_up answers from the ledger on a short tool budget (a follow-up needing >4 tool
# calls is really a fresh run). compare_with_rippling loads a one-time Rippling ledger.
FOLLOWUP_PROMPT_VERSION = "followup_v1"
FOLLOWUP_MAX_STEPS = 4
COMPARE_PROMPT_VERSION = "compare_v1"
RIPPLING_SLUG = "rippling.com"
_RIP_PREFIX = "RIP"  # relabel Rippling IDs in the combined digest so [CLM-001] can't collide


@dataclass
class RunResult:
    competitor: str
    brief: str
    ledger: list[Claim] = field(default_factory=list)
    steps: int = 0
    cost: CostAccumulator = field(default_factory=CostAccumulator)
    clarifying_question: str = ""
    canonical_claims: list[CanonicalClaim] = field(default_factory=list)
    session: Session | None = None  # live thread — the REPL resumes this for follow-ups


def _cached_system(prompt_text: str) -> list[dict]:
    """System prompt as one cached text block (prompt caching — the DRY(E) cost lever).

    The orchestrator system prompt is static across every turn, so caching it makes
    follow-ups like 'dig deeper on pricing' read from cache instead of re-billing the
    full prefix. cache_read tokens flow through Usage into the cost line + cache-hit rate.
    1h TTL (2x one-time write cost): the default 5-minute window expired while the user
    read the brief, so the first follow-up — the turn that showcases caching — always
    logged 0% hits.
    """
    return [
        {
            "type": "text",
            "text": prompt_text,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]


def _cached_tools(schemas: list[dict]) -> list[dict]:
    """Tool schemas with a cache breakpoint on the last tool (caches the whole tools block).

    Copies so we never mutate the dicts BaseTool.schema() returns.
    1h TTL (2x one-time write cost): the default 5-minute window expired while the user
    read the brief, so the first follow-up — the turn that showcases caching — always
    logged 0% hits.
    """
    if not schemas:
        return []
    cached = [dict(s) for s in schemas]
    cached[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return cached


async def _dispatch(
    tool: BaseTool,
    call_input: dict,
    session: Session,
    client: Any,
    extractor_prompt: str,
) -> tuple[str, list[Claim], Usage]:
    """Call a tool, extract claims, append to the session ledger.

    Returns (tool_result_digest, new_claims, extractor_usage). The digest (not the raw
    excerpt, not the full ledger) is what the orchestrator sees next turn — that's the
    D6/DRY(E) move.
    """
    result = await tool.run(**call_input)
    if isinstance(result, ToolFailure):
        return f"FAILED ({result.reason}) → suggestion: {result.suggestion}", [], Usage()

    if result.status == "empty" or not result.raw_excerpt:
        return "empty — no data returned from this source", [], Usage()

    raw_claims, usage = extract_claims_sync(result.raw_excerpt, client, extractor_prompt)
    new_claims: list[Claim] = []
    extracted_by = f"{EXTRACTOR_MODEL}/{EXTRACTOR_PROMPT_VERSION}"
    for raw in raw_claims:
        session.claim_counter[0] += 1
        cid = f"CLM-{session.claim_counter[0]:03d}"
        claim = claim_from_raw(raw, cid, session.competitor, tool.name, extracted_by)
        session.ledger.append(claim)
        new_claims.append(claim)

    digest = "\n".join(f"[{c.id}] ({c.category}) {c.statement}" for c in new_claims)
    return digest or "extracted 0 claims", new_claims, usage


async def _tool_cycle(
    session: Session,
    resp: Any,
    client: Any,
    system: Any,
    schemas: list[dict],
    registry: dict[str, BaseTool],
    extractor_prompt: str,
    max_steps: int,
) -> Any:
    """The plan->act->observe->re-plan cycle, shared by the initial run and follow-ups.

    Consumes the session step budget (per-session: follow-ups share the cap, so a
    chatty session can't run away). Returns the final (non-tool_use) response.
    """
    while tool_uses_of(resp) and session.steps < max_steps:
        tool_results = []
        for block in tool_uses_of(resp):
            session.steps += 1
            tool = registry.get(block.name)
            if tool is None:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"unknown tool: {block.name}",
                        "is_error": True,
                    }
                )
                continue
            digest, _new, ext_usage = await _dispatch(
                tool, block.input, session, client, extractor_prompt
            )
            session.cost = session.cost.add(EXTRACTOR_MODEL, ext_usage)
            console.print(f"[dim]  ✓ {block.name} → {escape(digest_preview(digest))}[/dim]")
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": digest,
                }
            )
        session.messages.append({"role": "user", "content": tool_results})
        resp = create(client, ORCHESTRATOR_MODEL, system, session.messages, schemas)
        session.cost = session.cost.add(ORCHESTRATOR_MODEL, Usage.from_sdk(resp.usage))
        session.messages.append({"role": "assistant", "content": resp.content})
    pending = tool_uses_of(resp)
    if pending:
        # Budget exhausted mid-cycle: the final assistant turn carries unexecuted tool_use
        # blocks. Append tool_result messages so the thread stays valid (an assistant
        # tool_use must be followed by tool_result, never a plain user message) and the
        # model knows the calls didn't run. No create()/cost impact — this only appends.
        session.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": "step budget exhausted — no more tool calls; answer from current data",
                    }
                    for b in pending
                ],
            }
        )
    return resp


def _ground_with_retry(
    text: str,
    session: Session,
    client: Any,
    system: Any,
    schemas: list[dict],
    canonical_claims: list[CanonicalClaim] | None = None,
    *,
    chat_mode: bool = False,
    claims: list[Claim] | None = None,
    regen_messages: list[dict] | None = None,
) -> str:
    """Grounding validator (CLAUDE.md §5): every cited [CLM-xxx]/[CAN-xxx] must map to a
    real claim. Brief mode: sub-gate (conf < 0.5) claims belong in the appendix/Test zone,
    not the body. Chat mode (follow-ups, compare): no sections, so sub-gate claims are
    allowed (the prompt requires in-line 'unverified' labeling) and only missing IDs fail.
    On failure, regenerate with the specific failures fed back (max 2 retries), then flag
    in-text. Deterministic check — never trusts the model to self-police.

    claims defaults to session.ledger; compare_with_rippling passes the combined
    (competitor + relabeled Rippling) claim set. regen_messages, when given, is the
    conversation context the retry regenerates within (chat mode); brief mode regenerates
    from the digest alone (the existing behavior)."""
    if claims is None:
        claims = session.ledger
    for attempt in range(3):
        report = check_grounding(text, claims, canonical_claims, chat_mode=chat_mode)
        if report.passed:
            break
        missing = ", ".join(report.missing_from_ledger) or "none"
        subgate = ", ".join(f.citation for f in report.subgate_in_body) or "none"
        supragate = ", ".join(f.citation for f in report.supragate_in_appendix) or "none"
        if attempt < 2:
            console.print(
                f"[yellow]  ⚠ grounding failed (try {attempt + 1}): "
                f"missing=[{missing}] subgate-in-body=[{subgate}] "
                f"supragate-in-appendix=[{supragate}] — regenerating[/yellow]"
            )
            move_clause = (
                ""
                if chat_mode
                else " and move EVERY conf < 0.5 claim to an '## Unverified signals' "
                "appendix at the end (never the body), and EVERY conf >= 0.5 claim "
                "OUT of that appendix into the appropriate body section"
            )
            feedback = (
                f"Your text failed grounding. Problems — cited IDs not in the ledger: "
                f"[{missing}]; sub-gate claims (confidence < 0.5) cited outside a permitted "
                f"zone: [{subgate}]; body-eligible claims (confidence >= 0.5) wrongly listed "
                f"under 'Unverified signals': [{supragate}]. Rewrite citing ONLY valid claim "
                f"IDs{move_clause}. "
                f"Valid claims:\n{ledger_digest(claims, canonical_claims)}"
            )
            retry_msgs = (
                list(regen_messages) + [{"role": "user", "content": feedback}]
                if regen_messages is not None
                else [{"role": "user", "content": feedback}]
            )
            resp = create_text(
                client,
                ORCHESTRATOR_MODEL,
                system,
                retry_msgs,
                max_tokens=SYNTHESIS_MAX_TOKENS,
                tools=schemas,
            )
            session.cost = session.cost.add(ORCHESTRATOR_MODEL, Usage.from_sdk(resp.usage))
            text = text_of(resp)
        else:
            console.print(
                "[red]  ⚠ grounding still failing after 2 retries — flagging in brief[/red]"
            )
            text = (
                f"{text}\n\n---\n⚠️ **Grounding warning:** failed automated grounding validation "
                f"(missing citations: [{missing}]; sub-gate claims in body: [{subgate}]; "
                f"body-eligible claims in appendix: [{supragate}]) — flagged for manual review."
            )
    return text


def _freshness_line(ledger: list[Claim]) -> str:
    """Deterministic 'data as of' stamp from the most recent evidence fetch.

    Stale CI is worse than none because readers trust it — every brief opens with
    when its underlying data was actually fetched. Empty string on an empty ledger.
    """
    fetched = [e.fetched_at for c in ledger for e in c.evidence]
    if not fetched:
        return ""
    return f"*Data as of {max(fetched).date().isoformat()} (most recent source fetch).*\n\n"


async def run_competitor(
    competitor: str,
    client: Any,
    tools: list[BaseTool],
    clarifying_answer: str | None = None,
    max_steps: int = STEP_BUDGET,
    start_monotonic: float | None = None,
    ask_user: Callable[[str], str] | None = None,
) -> RunResult:
    """Full run: intake + ONE clarifying Q -> tool_use cycle -> synthesis -> cost line.

    The clarifying question is answered by `ask_user` (interactive: a real prompt) when
    provided, else by the static `clarifying_answer` (one-shot mode: "both").
    """
    system = _cached_system(load_prompt(ORCHESTRATOR_PROMPT_VERSION))
    extractor_prompt = load_prompt(EXTRACTOR_PROMPT_VERSION)
    clustering_prompt = load_prompt("clustering_v1")
    schemas = _cached_tools([t.schema() for t in tools])
    registry = {t.name: t for t in tools}

    session = Session(competitor=competitor)
    session.messages.append({"role": "user", "content": f"analyze {competitor}"})

    # --- intake + clarifying question ---
    resp = create(client, ORCHESTRATOR_MODEL, system, session.messages, schemas)
    session.cost = session.cost.add(ORCHESTRATOR_MODEL, Usage.from_sdk(resp.usage))
    session.messages.append({"role": "assistant", "content": resp.content})

    clarifying_q = ""
    if not tool_uses_of(resp):
        answer: str | None = None
        if ask_user is not None:
            clarifying_q = text_of(resp)
            answer = ask_user(clarifying_q)
        elif clarifying_answer is not None:
            clarifying_q = text_of(resp)
            answer = clarifying_answer
            # One-shot mode: show the question WHEN it is auto-answered, not after the
            # run completes — post-hoc printing read as out-of-order output in the CLI.
            console.print(
                f"[dim]clarifying Q: {clarifying_q}[/dim]\n[dim](auto-answered '{answer}')[/dim]"
            )
        if answer is not None:
            session.messages.append({"role": "user", "content": answer})
            resp = create(client, ORCHESTRATOR_MODEL, system, session.messages, schemas)
            session.cost = session.cost.add(ORCHESTRATOR_MODEL, Usage.from_sdk(resp.usage))
            session.messages.append({"role": "assistant", "content": resp.content})

    # --- tool_use cycle ---
    resp = await _tool_cycle(
        session, resp, client, system, schemas, registry, extractor_prompt, max_steps
    )

    # --- D2 clustering: merge same-assertion claims across independent sources into
    # CanonicalClaims so the 0.9 corroboration tier fires (per-source extraction alone
    # never produces 2-source claims — the Gusto/Deel ledgers had zero 0.9 claims before
    # this). Singletons stay as flat Claims; canonical claims are saved alongside the
    # ledger (outputs/{slug}_canonical.json) and cited directly in the brief via
    # [CAN-xxx] (Phase 6, Step 4) — the enriched synthesis digest below includes them. ---
    if len(session.ledger) >= 2:
        canonical, cluster_usage = cluster_claims(session.ledger, client, clustering_prompt)
        session.canonical_claims = canonical
        session.cost = session.cost.add(CLUSTERING_MODEL, cluster_usage)
        if canonical:
            # Backlink: singleton Claims that got merged into a CanonicalClaim record
            # which one, so a flat Claim always shows its own corroboration status
            # (immutable update — model_copy, never mutate in place).
            member_to_canonical = {cid: c.id for c in canonical for cid in c.member_claim_ids}
            session.ledger = [
                claim.model_copy(update={"canonical_id": member_to_canonical[claim.id]})
                if claim.id in member_to_canonical
                else claim
                for claim in session.ledger
            ]
            n09 = sum(1 for c in canonical if c.confidence >= 0.9)
            console.print(
                f"[dim]  ✓ clustered {len(canonical)} canonical claims "
                f"({n09} at 0.9 corroboration)[/dim]"
            )

    # --- synthesis: always one explicit "write the brief" call (Phase 6, Step 4) ---
    # Previously this only happened on the step-budget-fallback path, so the model's
    # spontaneous mid-cycle text (the common case) never saw the enriched digest
    # (observed_vs_inferred/signal/canonical_id). One code path, every run, guarantees
    # the new decision-oriented sections have the data they need to be non-empty.
    synth_msg = [
        {
            "role": "user",
            "content": (
                "Research complete. Write the brief per the required section structure, "
                "citing [CLM-xxx] or [CAN-xxx] as appropriate.\n"
                f"{source_coverage_line(session.ledger)}\n"
                "Claims:\n"
                f"{ledger_digest(session.ledger, session.canonical_claims)}"
            ),
        }
    ]
    resp = create_text(
        client,
        ORCHESTRATOR_MODEL,
        system,
        synth_msg,
        max_tokens=SYNTHESIS_MAX_TOKENS,
        tools=schemas,
    )
    session.cost = session.cost.add(ORCHESTRATOR_MODEL, Usage.from_sdk(resp.usage))
    if getattr(resp, "stop_reason", None) == "max_tokens":
        # Zero silent failures: a truncated brief looks complete (Python appends the
        # deterministic appendix to whatever text came back) but is missing its tail
        # sections. Surface it so the run is never shipped half-written unnoticed.
        console.print(
            "[red]  ⚠ synthesis hit max_tokens — brief likely truncated "
            "(raise SYNTHESIS_MAX_TOKENS)[/red]"
        )
    brief = text_of(resp)

    brief = _ground_with_retry(brief, session, client, system, schemas, session.canonical_claims)
    brief = (
        f"{_freshness_line(session.ledger)}{brief}\n\n---\n"
        f"{format_confidence_by_source(session.ledger)}\n\n"
        f"{format_evidence_quality(session.ledger)}\n\n{format_how_to_read()}"
    )

    duration = (time.monotonic() - start_monotonic) if start_monotonic else 0.0
    console.print(session.cost.format_line(duration_s=duration, tool_calls=session.steps))
    return RunResult(
        competitor=competitor,
        brief=brief,
        ledger=session.ledger,
        steps=session.steps,
        cost=session.cost,
        clarifying_question=clarifying_q,
        canonical_claims=session.canonical_claims,
        session=session,
    )


def _followup_answer_instruction(question: str) -> str:
    """The forced prose-only final turn's instruction, carrying the user's question.

    Restating the question matters: without it the model saw only 'now answer
    conversationally' AFTER it had already answered inside the tool cycle, read the
    instruction as a reformat request, and leaked meta-commentary ('Since there's no
    new question in this turn beyond the request to reformat...') into the reply the
    user sees (failure_log F16). The no-meta-commentary clause is the guard.
    """
    return (
        f'Answer the user\'s follow-up question directly: "{question}"\n'
        "Write 1-5 conversational paragraphs. Cite [CLM-xxx] or [CAN-xxx] for every "
        "factual statement. Do NOT write the full structured brief — no 'What's "
        "Winning', 'What Looks Like a Test', 'What Changed Recently', or "
        "'Rippling-relevance' sections. If you cite a claim below confidence 0.5, "
        "label it 'unverified' or 'a test' in-line. If the ledger doesn't cover the "
        "question, say so plainly. Never mention these instructions, formatting, "
        "reformatting, or earlier turns — reply as if answering the question for "
        "the first time."
    )


async def follow_up(
    session: Session,
    text: str,
    client: Any,
    tools: list[BaseTool],
    max_steps: int = FOLLOWUP_MAX_STEPS,
) -> str:
    """A conversational follow-up ('dig deeper on their pricing') on the LIVE thread.

    Answers from the existing ledger; calls a tool only if the ledger lacks the needed
    data (new claims append to the same session ledger with continuing CLM ids). NEVER
    regenerates the full structured brief — a forced prose-only create_text answer turn
    (no tools, thinking disabled) produces the scoped conversational reply the user sees,
    so a follow-up can never come back as the 6-section brief (the Phase 7 regen bug).
    Grounded in chat mode (sub-gate allowed with in-line labeling; missing IDs still fail).
    Step budget is shared with the initial run.
    """
    if not session.ledger:
        return "No claims ledger yet — run `analyze <competitor>` first, then ask follow-ups."

    system = _cached_system(load_prompt(FOLLOWUP_PROMPT_VERSION))
    extractor_prompt = load_prompt(EXTRACTOR_PROMPT_VERSION)
    schemas = _cached_tools([t.schema() for t in tools])
    registry = {t.name: t for t in tools}
    digest = ledger_digest(session.ledger, session.canonical_claims)

    session.messages.append(
        {
            "role": "user",
            "content": (
                f"{text}\n\n[Current claims ledger — answer from this where you can]\n{digest}"
            ),
        }
    )
    resp = create(client, ORCHESTRATOR_MODEL, system, session.messages, schemas)
    session.cost = session.cost.add(ORCHESTRATOR_MODEL, Usage.from_sdk(resp.usage))
    session.messages.append({"role": "assistant", "content": resp.content})

    # _tool_cycle's guard is `session.steps < max_steps`, but session.steps is cumulative
    # across the whole session (initial run + follow-ups). A per-turn cap of N must be
    # expressed as `steps_at_entry + N` — else a follow-up after an 8-tool initial run can
    # never call a tool (8 < 4 is always False). Code-review P1 fix.
    steps_at_entry = session.steps
    resp = await _tool_cycle(
        session,
        resp,
        client,
        system,
        schemas,
        registry,
        extractor_prompt,
        steps_at_entry + max_steps,
    )

    # Forced scoped answer (Phase 7): the tool cycle may emit anything, including a
    # full-brief-shaped text. This prose-only call — no tools, thinking disabled —
    # produces the conversational reply the user actually sees, so a follow-up can NEVER
    # come back as the 6-section brief. The deterministic guard against the regen bug.
    resp = create_text(
        client,
        ORCHESTRATOR_MODEL,
        system,
        session.messages + [{"role": "user", "content": _followup_answer_instruction(text)}],
        max_tokens=SYNTHESIS_MAX_TOKENS,
        tools=schemas,
    )
    session.cost = session.cost.add(ORCHESTRATOR_MODEL, Usage.from_sdk(resp.usage))
    session.messages.append({"role": "assistant", "content": resp.content})

    answer = text_of(resp) or "(no answer produced — try rephrasing the question)"
    return _ground_with_retry(
        answer,
        session,
        client,
        system,
        schemas,
        session.canonical_claims,
        chat_mode=True,
        regen_messages=session.messages,
    )


def _relabel(claims: list, prefix: str) -> list:
    """Relabel claim IDs with a prefix so two ledgers can be merged in one digest without
    [CLM-001] collisions (competitor CLM-001 vs Rippling CLM-001). Immutable model_copy."""
    return [c.model_copy(update={"id": f"{prefix}-{c.id}"}) for c in claims]


async def compare_with_rippling(session: Session, client: Any) -> str:
    """Compare the current competitor's marketing strategy with Rippling's, citing BOTH
    ledgers. Rippling's ledger is built once (decision D2=A: `make run
    COMPETITOR=rippling.com`) and reused; its IDs are RIP--prefixed so they don't collide.
    Conversational answer (not a brief), grounded in chat mode against the combined set.
    """
    rippling = load_ledger(RIPPLING_SLUG)
    if not rippling:
        return (
            "No Rippling claims ledger found. Build one first:\n"
            f"  make run COMPETITOR={RIPPLING_SLUG}\nThen ask to compare again."
        )
    rippling_canonical = load_canonical_claims(RIPPLING_SLUG) or []
    rippling_r = _relabel(rippling, _RIP_PREFIX)
    rippling_canon_r = _relabel(rippling_canonical, _RIP_PREFIX)

    combined_claims = list(session.ledger) + rippling_r
    combined_canonical = list(session.canonical_claims) + rippling_canon_r

    system = _cached_system(load_prompt(COMPARE_PROMPT_VERSION))
    digest = (
        f"## Competitor: {session.competitor}\n"
        f"{ledger_digest(session.ledger, session.canonical_claims)}\n\n"
        f"## Rippling\n"
        f"{ledger_digest(rippling_r, rippling_canon_r)}"
    )
    compare_msg = {
        "role": "user",
        "content": (
            f"Compare {session.competitor}'s marketing strategy with Rippling's. Answer "
            f"conversationally (1-6 paragraphs). Cite [CLM-xxx]/[CAN-xxx] for the competitor "
            f"and [{_RIP_PREFIX}-CLM-xxx]/[{_RIP_PREFIX}-CAN-xxx] for Rippling, for every "
            f"factual statement. Surface shared strategies AND meaningful differences. Do "
            f"NOT write the full structured brief. If a claim is below confidence 0.5, label "
            f"it 'unverified' in-line.\n\n{digest}"
        ),
    }
    # Put the comparison on the live thread (code-review P1 #3): a later "based on that
    # comparison..." follow-up must see it. Grounding retry then regenerates within the
    # full thread (regen_messages=session.messages), not from a one-shot message.
    session.messages.append(compare_msg)
    resp = create_text(
        client, ORCHESTRATOR_MODEL, system, session.messages, max_tokens=SYNTHESIS_MAX_TOKENS
    )
    session.cost = session.cost.add(ORCHESTRATOR_MODEL, Usage.from_sdk(resp.usage))
    session.messages.append({"role": "assistant", "content": resp.content})
    answer = text_of(resp) or "(no answer produced)"
    return _ground_with_retry(
        answer,
        session,
        client,
        system,
        [],
        combined_canonical,
        chat_mode=True,
        claims=combined_claims,
        regen_messages=session.messages,
    )

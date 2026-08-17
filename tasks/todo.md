# tasks/todo.md — Competitive Intel Agent

Build tracking. Check items as they complete. This file is the execution checklist.

Legend: 🟢 no API key needed · 🔑 needs API key (human-gated) · ⭐ differentiator (NEVER cut)

## Phase 0 — scaffold + seam (hour 0-1) · ✅ criterion: `make run` prints hello-loop · DONE 2026-07-14

- [x] 🟢 Repo structure: agent/prompts, tools, ledger, evals/{fixtures,golden}, outputs, tasks
- [x] 🟢 requirements.txt (anthropic, httpx, pydantic, python-dotenv, rich, pytest, ruff)
- [x] 🟢 .env.example with all key placeholders + mode-aware validation stub
- [x] 🟢 Makefile: make run / make demo / make eval (uses `uv run --with-requirements`)
- [x] 🟢 ledger/models.py — Claim, Evidence (+utm_params D6), SourceResult, ToolFailure, CanonicalClaim (D2); ≥1 evidence enforced; frozen/immutable
- [x] 🟢 ledger/confidence.py — deterministic rubric incl. D5 ads branch (0.3+0.2+0.1+0.1, cap 0.7, perf-never-0.9) + confidence_trace; <0.5 gate → appendix
- [x] 🟢 tools/_base.py — BaseTool abstract (async run → SourceResult | ToolFailure)
- [x] 🟢 tools/_transport.py — LiveTransport (timeout+retry+backoff) / ReplayTransport (fixture lookup by request key) [D1/D3 seam]
- [x] 🟢 agent/loop.py skeleton — hello-loop; cost hook (response.usage) wired from Phase 1
- [x] 🟢 `make run` prints hello-loop · `make eval` green (11/11) · ruff clean
- [ ] 🔑 Acquire + smoke-test API keys: ANTHROPIC_API_KEY, SCRAPECREATORS_API_KEY, FIRECRAWL_API_KEY, EXA_API_KEY, APIFY_API_TOKEN, OPENAI_API_KEY (or GEMINI) — record picks in DECISIONS.md (D4 model pin refresh, 30-min time box) — **HUMAN-GATED, blocks Phase 1 live runs**

## Phase 1 — core loop + two hardest sources (day 1) · ✅ ugly Gusto brief exists

- [x] 🟢⭐ Orchestrator tool_use loop: cost accumulation + working-summary (D6) + step-budget cap 35 + skip-empty + ONE clarifying-Q gate; integration test with stubbed Anthropic client (CLAUDE.md §15) · streaming + prompt caching wired at live-client time
- [~] 🔑⭐ tools/crawl_site.py — Firecrawl /map then /scrape; pick-urls heuristic; skip-empty on failed page; mocked + fixture test DONE · 🔑 live wiring needs FIRECRAWL_API_KEY
- [~] 🔑⭐ tools/meta_ads.py — ScrapeCreators; zero-ads gotcha as `status="empty"` (the Loom moment); D5 inputs (start_date/regions) captured; mocked + fixture test DONE · 🔑 live wiring + Apify fallback raw httpx needs keys
- [x] 🟢⭐ Haiku extractor — batched excerpt per call, per-claim source_url (D7); versioned prompt agent/prompts/extractor_v1.md; _parse_claims + stub test DONE
- [~] 🟢⭐ ledger writes — in-memory Claim construction with deterministic confidence (ledger/build.py) DONE · persist/intel.json export pending (Phase 3 with re-run diff)
- [x] 🟢 agent/cost.py — tokens→$ per model, cache hit rate, format cost line; 5 unit tests DONE
- [x] 🟢 ledger/models.py Confidence rubric + D5 ads branch (smoke tests) + CanonicalClaim (D2) + Evidence.utm_params (D6) DONE in Phase 0
- [ ] 🔑 First ugly Gusto brief + intel.json — BLOCKED on API keys (ANTHROPIC + FIRECRAWL + SCRAPECREATORS)

## Phase 2 — breadth + ledger + gates (day 2 AM) · ✅ brief is grounded, gated, cited

- [ ] 🔑 tools/google_ads.py — ScrapeCreators Google transparency (SerpApi fallback); UTM (D6); tests
- [ ] 🟢⭐ tools/wayback_diff.py — CDX API, 90/180d snapshots, diff hero/headline/nav; no-snapshot→press degrade; tests
- [ ] 🔑 tools/news_press.py — Exa date-filtered search; tests
- [ ] 🔑 tools/jobs_signals.py — Firecrawl careers page; tests
- [ ] 🔑 tools/g2_reviews.py — public G2/Capterra; tests
- [ ] 🔑 tools/social_posts.py — LinkedIn Ad Library + blog/YouTube RSS; tests
- [ ] 🟢⭐ ledger/cluster.py — Haiku-assisted CanonicalClaim (D2); versioned prompt; fixture test
- [ ] 🟢⭐ ledger/grounding.py — inline [CLM-xxx] regex validate against ledger; regenerate max 2 → flag (D3); tests
- [ ] 🟢⭐ ledger/url_health.py — build-time guard (dead+Wayback swap / dead-no-snapshot drop) + LIKELY_HALLUCINATED (D4); async fan-out + per-run cache; tests
- [ ] 🟢⭐ confidence gate enforced: <0.5 → unverified appendix, never brief body

## Phase 3 — conversation + second competitor (day 2 PM) · ✅ both briefs exist; follow-ups work

- [ ] 🟢 Conversational layer: clarifying Qs, follow-ups reuse ledger, fresh-competitor re-entry
- [ ] 🟢 Re-run diff: prior ledger → report what's new; no-prior → say so
- [ ] 🟢 Session persistence per competitor
- [ ] 🟢 outputs/{competitor}_trace.jsonl — per-run tool-call trace artifact (D8)
- [ ] 🔑 Deel run — global/EOR positioning must surface (golden property)

## Phase 4 — evals + polish + ship (day 3) · ✅ `make eval` green; ship

- [ ] 🟢⭐ Layer 1 evals — schema validity, ≥1 evidence, timestamps parse, confidence recompute, grounding check, no sub-gate in body, url_health classification
- [x] 🔑⭐ Layer 2 judge — GPT-4o-mini/Gemini-2.5-Flash (direct calls, not Batch); faithfulness/specificity/hallucination + brief rubric. `make eval-judge` + evals/judge.py + evals/test_judge.py (12 tests). Batch API remains the production note (DECISIONS §15).
- [ ] 🟢⭐ Layer 3 trajectory — step-budget, skip-empty, clarifying-Q, no loops
- [ ] 🟢⭐ Golden set — Gusto (SMB/price-first), Deel (global/EOR), BambooHR (breadth); property-based
- [x] 🟢⭐ evals/failure_log.md — seeded with real dead URL (D7) + failures caught reading outputs
- [ ] 🟢 Demo mode wiring — ReplayTransport replays Phase 1-2 fixtures; mode-aware key validation (D3)
- [ ] 🟢 DECISIONS.md — single-orchestrator (cite 15x tokens), no LangGraph, Meta US-ads gotcha, Exa-over-Tavily, repos studied + divergences, Firecrawl credit strategy, model pins (D4), commercial landscape (D7), ToS note, 2-week roadmap
- [ ] 🟢 README — 60-sec quickstart, architecture diagram, cost math (~$X/competitor → 20 competitors ≈ $Y/mo), demo GIF
- [ ] 🟢 Final clean runs: Gusto + Deel; record Loom (5-10 min); email Will + Megan before 72h

## Cut order (if time bites)
D6(UTM) → D7(docs bundle) → D8(trace) → g2_reviews → jobs_signals → re-run diffing → social_posts.
NEVER cut: evals, claims ledger, wayback_diff, two competitors, cost report, DECISIONS.md, D3 demo seam, D4 model pins.

## Review (fill in when done)
- Phase 0: ___ · Phase 1: ___ · Phase 2: ___ · Phase 3: ___ · Phase 4: ___
- `make eval` status: ___ · Final cost/competitor: $___
- Loom recorded: ___ · Shipped to Will + Megan: ___

## Phase 5 — interactive REPL + name/domain input (2026-07-14, spec: docs/superpowers/specs/2026-07-14-interactive-repl-design.md)

- [x] agent/resolve.py — deterministic name→domain (dot→domain; slug.com HEAD; Sonnet fallback; confirm in REPL) + prompts/resolver_v1.md
- [x] evals/test_resolve.py — passthrough, URL strip, spaced name, LLM fallback, unresolved
- [x] agent/session.py — Session dataclass; run_competitor operates on it; ask_user callback; follow_up() on live thread with grounding
- [x] agent/repl.py — deterministic router: analyze/follow-up/quit; per-turn error wrap; cumulative cost on exit
- [x] loop.py — refactor to Session + ask_user; run(None) → REPL; one-shot mode unchanged
- [x] evals/test_session.py — thread persists across follow-ups, ledger grows, ask_user once, new-competitor resets
- [x] make eval green — 141 passed, 1 skipped (129 existing + 12 new)
- [ ] NOTE (pre-existing gap found during refactor): `make demo` sets INTEL_MODE=demo but run() builds LiveTransport — ReplayTransport fixture replay is not wired into the CLI. Fix or soften the README demo claim before shipping.

## Phase 6 — synthesis upgrade: decision layer (2026-07-15)

- [x] Step 1: ledger/models.py fields (Evidence.first_seen/last_seen; Claim.observed_vs_inferred/signal/signal_trace/canonical_id; CanonicalClaim.signal/signal_trace) + ledger/signal.py (classify_ad_signal, classify_corroboration_signal) + fix 5 test files' direct Claim() construction
- [x] Step 2: extractor_v2.md (first_seen/last_seen passthrough) + build.py wiring (score_ads_performance live, observed_vs_inferred, signal) + meta_ads.py docstring fix
- [x] Step 3: clustering.py sets CanonicalClaim.signal + loop.py backfills Claim.canonical_id post-clustering
- [x] Step 4: grounding.py accepts [CAN-xxx] + loop.py always-explicit synthesis w/ enriched digest + ledger/summarize.py confidence-by-source table + orchestrator_v2.md (What's Winning / What Looks Like a Test / What Changed Recently sections)
- [x] Step 5: test_signal.py, test_build.py, test_clustering.py, test_grounding.py extensions; update test_loop.py/test_session.py/test_trajectory.py/golden for new synthesis call; make eval green
- [x] Regenerate Gusto brief live, spot-check new sections + JSON fields (Deel/BambooHR deferred per user — golden tests skip cleanly)

### Phase 6 follow-on fixes (found during live Gusto regen — 2026-07-15)

- [x] **build.py: default ad `last_seen` to crawl time when omitted.** Extractor copies `start_date` (first_seen) but, told never to invent dates, omits last_seen for active ads → signal branch never fired (all ad claims had signal=None). Defaulting last_seen=fetched_at is an observation (ad still in the active-ads library as of crawl), not a guess. Added injectable `now` param for deterministic tests; `_parse_date` now always returns tz-aware (UTC) datetimes to avoid naive/aware subtraction TypeError.
- [x] **loop.py: prose-only synthesis call disables thinking + offers no tools.** Claude Sonnet-5 emits thinking blocks by default; `max_tokens` is the combined thinking+text budget, so long-form synthesis thinking consumed the entire 4096 cap (stop_reason=max_tokens, zero text blocks → blank brief). `create_text` passes `tools=[]` + `thinking={"type":"disabled"}`. Helpers moved to agent/llm.py to keep loop.py <400 lines.
- [x] ScrapeCreators credits exhausted mid-Phase-6 (HTTP 402 on meta_ads/google_ads); user rotated a fresh API key → ad claims flow restored, signals populate (likely_winner/possible_test).

### Phase 6 review
- Steps 1–5: complete · `make eval`: **159 passed, 2 skipped** (Deel/BambooHR golden skip — no ledger) · ruff clean
- Gusto live regen: 310s · 8 tools · $0.49 · 18 canonical claims · brief has all 6 sections (What's Winning / What Looks Like a Test / What Changed Recently / Rippling-relevance / Unverified signals / Confidence by Source) · 14 CAN-xxx + 34 CLM-xxx citations · grounding: all cited IDs valid (no hallucinations), sub-gate claims flagged honestly after 2 retries
- Ad signals now populate: 26/26 ad claims classified (likely_winner/possible_test) — was 0 before the build.py fix
- Deferred: Deel + BambooHR live regen (user scope cut) · Apify/SerpApi ad fallbacks still unwired (spec gap, tracked)

## Phase 6.1 — brief trustworthiness pass (2026-07-15)

Reviewer (Will) verdict: "strong near-final output" but "impressive vs trustworthy" hinged on confidence handling + grounding. Six asks, all HOLD-SCOPE polish.

- [x] Grounding carve-out: "What Looks Like a Test" is a permitted hypothesis zone for sub-gate claims (it is *defined* as possible_test ads → 0.3 conf → sub-gate by construction). Quarantined out of What's Winning / What Changed Recently / Rippling-relevance. (ledger/grounding.py)
- [x] Prompt v2.1: hypothesis framing on every Test bullet; dedup Test/Unverified; sub-gate banned from Winning/Recency/Rippling-relevance; "What Changed Recently" narrowed to strategic shifts; winning/test rule stated in-brief. (agent/prompts/orchestrator_v2.md)
- [x] Deterministic winning_score (0-100: corroboration 40 + persistence 30 + recency 30) on CanonicalClaim, shown in digest + required in What's Winning. (ledger/signal.py, models.py, clustering.py, agent/digest.py)
- [x] Deterministic "## Evidence quality" (primary vs secondary split + body-eligible count) + "## How to read this brief" (rules + grounding guarantee) appended to every brief. (ledger/summarize.py, agent/loop.py)
- [x] DECISIONS.md §16 documents the gate + grounding + winning-score model.
- [x] Gusto live regen: 293s · 8 tools · $0.44 · 16 canonical claims scored (30-80) · grounding self-corrected on try 1 → final passes cleanly (0 sub-gate-in-body, no warning) · What Changed Recently is 4 strategic shifts + collapsed launches · Test/Unverified deduped · `make eval` 171 passed, 2 skipped.

## Phase 7 — conversational follow-ups + comparative mode (2026-07-15, /plan-eng-review)

**Problem:** after `make run` produces a brief, follow-ups like "dig deeper on the pricing"
regenerate the FULL brief (72s · 9 tools · $0.66) instead of answering from the existing ledger.
"run this again for gusto" (no `analyze` prefix) mis-routes to a follow-up on the OLD competitor;
"what strategies do Rippling and X share" has no route. Root cause = a composition bug, not
missing capability — every primitive the fix needs already exists and is used by `run_competitor`.

**Decisions:** D1 = deterministic keyword router (fall-through to `follow_up` enables any question);
D2 = one-time Rippling ledger (`make run COMPETITOR=rippling.com` once, reuse forever).

### Layer 1 — router intent classification (agent/repl.py) [D1=A]
- [ ] Replace the one-line `starts_new` branch (repl.py:201) with a deterministic dispatcher:
      `analyze X` / bare domain / no session → `_analyze`; `run again [for] X` / `again X` /
      `redo` / `rerun` → `_analyze` (resolve X via `resolve_competitor`); `compare [with|to] rippling`
      / `vs rippling` / `rippling` + similar/same/compare → `compare_with_rippling`; quit/exit → break;
      everything else → `follow_up`.
- [ ] `evals/test_router_intent.py` — each route asserted deterministically (no LLM, no network).
- [ ] **User-contribution spot (learning mode):** the keyword/regex pattern table mapping natural
      phrases → intents is a UX call about how YOU'll phrase run-again vs compare vs a question.
      ~10 lines in the router. I'll scaffold the dispatcher and leave the pattern table for you.

### Layer 2 — `follow_up()` rewrite (agent/loop.py)
- [ ] Surface `ledger_digest(session.ledger, session.canonical_claims)` in the follow-up turn so the
      model sees what's already known (in-memory `session.ledger` already persists across turns —
      `follow_up` just never formatted it).
- [ ] New `agent/prompts/followup_v1.md`: answer conversationally from the ledger; cite `[CLM-xxx]`/
      `[CAN-xxx]`; call a tool ONLY if the ledger lacks the data (name the tool — e.g. `crawl_site
      /pricing`); NEVER regenerate the full brief; keep the answer scoped to the question; label
      sub-gate claims as unverified/test in-chat.
- [ ] Short tool cycle (`max_steps≈4`); end on `create_text()` (tools=[], thinking disabled) → scoped
      chat answer, not the 6-section brief.
- [ ] `_ground_with_retry(..., chat_mode=True)` + `check_grounding(..., chat_mode=True)`: relax the
      "required 6 sections" rule for chat; still ban un-labeled sub-gate claims stated as fact.
- [ ] REGRESSION `test_follow_up_does_not_regen_full_brief` — the exact behavior that broke (asserts
      the answer lacks ≥2 of the 6 sections + is short).
- [ ] `test_follow_up_scoped_not_regen` (messages include a digest; final call is `create_text` w/
      tools=[]), `test_follow_up_grows_ledger_only_when_tool_called`, `test_follow_up_empty_ledger_defends`,
      `test_grounding_chat_mode`.

### Layer 3 — comparative mode (agent/loop.py + agent/prompts/compare_v1.md) [D2=A]
- [ ] Build the one-time Rippling ledger: `make run COMPETITOR=rippling.com` →
      `outputs/rippling.com_intel.json` + `outputs/rippling.com_canonical.json`.
- [ ] `compare_with_rippling(session, client)`: `load_ledger("rippling.com")`, build a combined
      digest (competitor + Rippling canonical claims), `compare_v1.md` prompt, synthesize with
      citations from BOTH ledgers; ground with `chat_mode=True`.
- [ ] `test_compare_with_rippling_cites_both`, `test_compare_no_rippling` (no Rippling ledger →
      conversational prompt to run `make run COMPETITOR=rippling.com` first).

### Layer 4 — live acceptance gate (lessons.md meta-lesson: stubs don't catch this)
- [ ] Marked live test (skip without `ANTHROPIC_API_KEY`): real follow-up on the Gusto session →
      no full-brief regen + a cited scoped answer.
- [ ] `make eval` green; `ruff check .` clean.
- [ ] Live end-to-end smoke: "dig deeper on the pricing" → scoped, cheap, cited; "compare with
      rippling" → cites both ledgers.

### NOT in scope (tracked as TODOs, not done here)
- Cross-PROCESS session resume (reload on-disk ledger into a session after REPL restart). In-memory
  session already persists across turns within a process — the bug was never persistence.
- `api_spend=$0.00` (data-API cost never accrued in the loop — `format_line` still prints `APIs: $0.00`).
- `ReplayTransport` wired into the CLI for `make demo` (pre-existing gap, Phase 5 note line 84).

### Review
- **Layer 1 (router):** done — deterministic `classify_turn` (regex `_RUN_AGAIN_RE` +
  `_is_rippling_compare`) + `test_router_intent` (22 tests).
- **Layer 2 (follow_up):** done — surfaces ledger digest, `followup_v1.md`, forced
  `create_text` scoped answer, chat-mode grounding, per-turn step budget. 6 Phase-7 tests
  incl. the regen guard + the cumulative-budget regression.
- **Layer 3 (compare):** done — `compare_with_rippling` + `compare_v1.md` + RIP-ID relabel
  + `load_canonical_claims`; the answer lands on the live thread. 3 tests. (Live compare
  pending the one-time Rippling ledger build.)
- **Layer 4 (live gate):** done — `make eval-live` PASSED twice; real follow-up on the
  persisted Gusto ledger returns a scoped, cited, conversational answer (no regen).
- **`make eval`:** 220 passed, 3 skipped (2 golden + live gate) · ruff check + format clean.
- **Follow-up cost (live):** ~$0.01, 2 Sonnet calls, 0 tools — vs the old 72s · 9 tools · $0.66.
- **Verification round:** 2 parallel reviewers (code-review + adversarial prober) found 5
  real issues the green stubbed suite missed (a cumulative-step-budget bug that dropped
  every follow-up tool call in prod, the "run this again" phrasing gap, the "rippling"
  over-match, the compare answer not on the live thread, orphaned tool_use blocks). All
  fixed + regression-tested. Pattern logged in `tasks/lessons.md`.

### Remaining (user-gated, not blocking)
- [ ] Build the one-time Rippling ledger: `make run COMPETITOR=rippling.com` (~$0.44, 5 min).
      Required for `compare with rippling` to answer with citations (D2=A). Without it the
      compare path returns a "build one first" prompt (tested in test_compare.py).
- [ ] Optional: live compare smoke after the Rippling ledger exists.

## Phase 8 — above-and-beyond pass: deliverable completeness + honesty (2026-07-15)

Gap analysis vs. Will's assignment: the brief *quality* already exceeded the ask; the caps
were (1) only one competitor brief shipped, (2) a mid-session "switch to a new competitor"
phrasing mis-routed to a follow-up (the live-transcript Workday bug), (3) failure_log.md
missing (NEVER-cut), (4) README/Status overstates shipped reality, (5) `APIs: $0.00` false
precision.

### Tier 1 — code/docs (no live keys; DONE)
- [x] **New-competitor routing fix** — `classify_turn(text, has_session, current_competitor)`
      + `_match_new_competitor` (explicit "different/new/another company called X",
      "switch to X", bare domain as the whole turn, "now do/analyze/run X.com"). High-precision:
      topic follow-ups ("do the same thing for their pricing") stay follow-ups. 13 new tests
      in evals/test_router_intent.py incl. the user's literal Workday phrasing. The bug that
      produced an unscored, ungrounded, un-persisted Workday half-run is fixed (failure_log F1).
- [x] **evals/failure_log.md** — NEVER-cut artifact, 12 real failure modes from lessons.md +
      the live transcript (F1–F12), each with fix/status. 8 fixed, 1 mitigated, 3 tracked.
- [x] **Layer-2 cross-family judge** — evals/judge.py (raw httpx, OpenAI-or-Gemini,
      stratified-sample faithfulness/specificity/hallucination + brief rubric), agent/prompts/
      judge_v1.md, evals/test_judge.py (12 tests), `make eval-judge` target. Skips cleanly
      with no judge key. Makes the README's 3-layer claim literally true.
- [x] **README + .env.example honesty pass** — corrected status (Gusto shipped; Deel one live
      run away), test count (248 collected / 245 passed / 3 skipped), real cost range
      ($0.42 Gusto / $1.17 Workday / ~$0.01 follow-ups vs the stale $0.20), softened the
      `make demo` offline-replay claim (ReplayTransport not wired — failure_log F5), reframed
      Layer-2 as implemented.
- [x] **Honest cost line** — `APIs: $0.00 (free tier)` when api_spend is 0; real number when
      a paid fallback accrues. test_cost.py updated.

### Tier 2 — user-gated live runs (DONE)
- [x] `make run COMPETITOR=deel.com` — shipped deel.com_brief.md + _intel.json +
      _canonical.json (63 claims, 4 global/EOR positioning claims). TestDeel PASSES
      (global/EOR surfaced). Proves "any competitor, not hardcoded." ✓ done by user.
- [x] `make eval-judge` for gusto.com + deel.com — outputs/{gusto,deel}.com_judge.json
      written (Gemini 3.5 Flash; Gusto 1.00/0.92/0% hall; Deel 0.97/0.84/0% hall). ✓
- [ ] (optional, Loom) `make run COMPETITOR=rippling.com` — enables `compare with rippling`
      to cite both ledgers live. Still pending.

### Live fixes during the judge run (2026-07-15)
- `select_provider` now skips .env.example placeholder keys (`sk-...`, len<20) so a copied
  template with a stale OPENAI_API_KEY placeholder doesn't shadow a real GEMINI_API_KEY and
  401 (lessons.md L2 "key is set ≠ key is valid" rule, applied to the judge). 4 new tests.
- `_GEMINI_MODEL` bumped `gemini-2.5-flash` → `gemini-3.5-flash` (2.5-flash is deprecated
  for new keys: 404 "no longer available to new users" as of 2026-07).
- `_gemini_call` auth moved from `?key=` query param to `x-goog-api-key` header, and
  `_with_retry` error messages no longer embed the request URL — so a failed call can never
  leak the API key into logs/terminal (it did once during diagnosis; key should be rotated).

### Review
- `make eval`: **250 passed, 2 skipped** (BambooHR golden + live follow-up gate) · ruff
  check + format clean.
- Two competitor briefs shipped + cross-family-judged. Layer-2 claim is literally true.
- Live gate PENDING: REPL `analyze gusto.com` → `Now do the same thing for a different
  company called Deel` → must route fresh (the F1 routing fix). Deel was run via one-shot
  `make run COMPETITOR=deel.com`, not via the in-session switch phrase — that path is still
  unit-tested (13 tests) but not yet exercised live end-to-end.
- Brief synthesis pipeline, confidence rubric, and grounding validator untouched.

---

# Plan: post-live-run fixes + output usefulness upgrade (2026-07-15)

Source: live BambooHR/Gusto run evaluation + web research (battlecard anatomy, BLUF,
ICD-203 estimative language, ad-longevity thresholds). No code until user confirms.

## Phase 0 — Restore & safeguard (~15 min)
- [x] 0.1 `git restore outputs/gusto.com_brief.md outputs/gusto.com_canonical.json outputs/gusto.com_intel.json` — recover polished shipped outputs clobbered by the thin re-run (Wayback 503 ×2, Meta empty)
- [x] 0.2 Overwrite protection: archive prior outputs to `outputs/history/{slug}/{timestamp}/` before writing new ones; print a note (repl.py write site)

## Phase 1 — Trust bugs (deterministic-gate story) (~2 h, TDD)
- [x] 1.1 grounding.py: add symmetric check — supra-gate (>= 0.5) claims cited inside the "Unverified signals" zone fail grounding (`supragate_in_appendix` findings; extend `passed`); wire into `_ground_with_retry` regen message
- [x] 1.2 Synthesis prompt (version bump): claims >= 0.5 must never be listed under Unverified signals
- [x] 1.3 Follow-up meta-leak: rewrite `_FOLLOWUP_ANSWER_INSTRUCTION` (loop.py:402) — restate the user's question in the forced turn; forbid meta-commentary about instructions/reformatting. Verify with `make eval-live`
- [x] 1.4 Per-turn cost lines: snapshot cost/steps at command entry in repl (repl.py:427/438/451), print per-turn delta + "(session total …)"; fix tool_calls to per-turn; add delta support in cost.py; remove banned "count the nickels" phrase (cost.py:1); verify sonnet-5 pricing (cost.py:25 TODO)

## Phase 2 — Output usefulness upgrade (research-driven) (~2-3 h)
Low-effort (synthesis prompt + template, version bumps):
- [x] 2.1 BLUF "Verdict" section at top: 3-5 key judgments, each cited + one concrete recommended action
- [x] 2.2 "Data as of <date>" header; explicit date ranges in What Changed Recently
- [x] 2.3 ICD-203-style estimative labels rendered deterministically from confidence ("high confidence — 2+ independent sources")
- [x] 2.4 Ad-longevity thresholds as deterministic labels (<14d testing / 21-45d acceptable / 45+d likely winner / multi-format confirmed)
- [x] 2.5 Action-per-insight: every What's Winning / Changed bullet ends "→ For Rippling: <action>"; cut no-action items
Medium-effort:
- [x] 2.6 Rippling-relevance → battlecard blocks: Where they win / Where we win, Landmines (2-3 discovery questions), Objection handling; each angle tagged with target segment; include honest "where they're stronger" + reframe
- [x] 2.7 Campaign test hypotheses: 5-8 ranked "We believe X will outperform because [longevity/white-space signal]" from ad claims
- [x] 2.8 Appendix noise: extractor prompt bump (skip culture/founding/employer-brand claims); deterministic filter — constituent CLMs of a cited CAN don't re-list in appendix
- [x] 2.9 Wayback prominence: What Changed Recently must cite wayback claims when present; explicit "no archive evidence" note otherwise

## Phase 3 — Monitoring/diff credibility (~1-1.5 h)
- [x] 3.1 Robust diff (persist.py:146): canonical-level diff + token-Jaccard fuzzy match (>= 0.6 = unchanged); report "pillars unchanged / new / no-longer-observed"
- [x] 3.2 README monitoring claim reworded to match measured behavior

## Phase 4 — Eval & docs closure (~1-1.5 h)
- [x] 4.1 Unskip BambooHR golden (outputs now exist); `make eval` green
- [x] 4.2 `make eval-urls`: URL-health sweep over all outputs/*_intel.json, written report; README "measured" claim updated
- [x] 4.3 One-shot ordering: print clarifying Q + "(auto-answered 'both')" BEFORE tool progress (repl.py:491)
- [x] 4.4 Doc accuracy pass: DRY(E) = "Don't Repeat Your Embeddings" attribution; ODR evals claim reword; arXiv figures → verified 3-13% / 5-18% ranges
- [x] 4.5 failure_log.md entries F14-F17 (supra-gate appendix, clobbered outputs, cumulative cost line, follow-up meta-leak); clean stale todo items; add review section
- [x] 4.6a Final: `make eval` green (264 passed, 1 skipped = opt-in live gate; ruff clean)
- [ ] 4.6b One live run to regenerate a brief in the new format (~$0.45, user's call which competitor)

Out of scope (user-owned): Loom recording; .env keys stay per user instruction.

## Review (2026-07-15 fixes + upgrade pass)

**Shipped:** all of Phases 0-4 except the optional live regeneration run (4.6b).
- Trust: symmetric gate check (F14) with regen wiring + 5 tests; archive-before-
  overwrite (F15) + 2 tests; per-turn REPL cost lines via CostAccumulator.minus (F16)
  + 2 tests; follow-up meta-commentary guard (F16); fuzzy re-run diff (F17) + 2 tests.
- Output usefulness (research-backed): orchestrator_v3 (Verdict/BLUF, action-per-
  insight, battlecard Rippling-relevance, campaign test hypotheses, wayback-first
  recent-changes, estimative labels); extractor_v3 (marketing-relevance filter);
  deterministic estimative_label + longevity_label rendered into the digest;
  "data as of" freshness stamp; how-to-read updated to match.
- Evals/docs: BambooHR golden unskipped (passes on the live-run output); make
  eval-urls sweep over shipped ledgers; sonnet-5 pricing verified ($2/$10 intro to
  2026-08-31); DECISIONS accuracy fixes (ODR evals claim, arXiv 3-13% range, DRY(E)
  attribution note); README honesty fixes (demo-not-offline, follow-up cost range,
  quickstart command); failure_log F14-F17.
- Suite: 264 passed, 1 skipped (opt-in live gate), ruff clean.

**Deliberately NOT done (user-owned):** Loom recording; .env keys left in place per
user instruction; live brief regeneration in the new format (4.6b — needs a paid run;
NOTE: the shipped Gusto/Deel briefs still use the v2 format until this runs).

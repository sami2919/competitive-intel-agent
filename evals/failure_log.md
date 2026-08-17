# Failure Log — real failure modes caught by reading outputs

> The honest counterpart to the eval suite. CLAUDE.md §7/§11 require this file to be
> populated by *actually reading outputs*, not by guessing. Every entry below is a real
> failure observed during a live run or while reading a produced brief/ledger, with the
> fix or current status. Product failure modes live here; dev-process lessons live in
> `tasks/lessons.md`. The arXiv 2604.03173 baselines (3.5% hallucinated claims, 10.1%
> non-resolving URLs) are the reference for the URL-health and grounding rows.

## Format

- **ID — title** · status (fixed / mitigated / tracked)
- What happened (observed, with the run/competitor it surfaced on)
- Root cause
- Fix or current status + how it's verified

---

## F1 — "Switch to a new competitor" mis-routed to a follow-up · fixed

**What happened:** In a live REPL session on `gusto.com`, the user typed
`Now do the same thing for a different company called Workday`. The deterministic router
(`classify_turn`) saw no `analyze` prefix, no run-again keyword, and no "rippling" — and
because a Gusto session was live, it fell through to `followup`. The result: a half-scoped
conversational answer that pulled 4 tools (crawl, meta_ads, google_ads, wayback_diff) but
**never ran clustering, grounding, or a synthesis pass**, never wrote a brief, and the agent
itself admitted "all of the Workday claims above are freshly pulled and haven't gone through
the same corroboration/scoring pass… no confidence scores or CAN-xxx clusters assigned."

**Root cause:** the router didn't know which competitor the session was on, and its
fall-through was `followup` for any unrecognized text with a live session. Natural
new-competitor phrasings had no branch.

**Fix:** `classify_turn` now takes `current_competitor`; a new `_match_new_competitor`
detector routes explicit switch phrasings ("a different/new/another company called X",
"switch to X", a bare domain as the whole turn, "now do/analyze/run X.com") to a fresh
`analyze` — which gets the full pipeline + a persisted brief. High-precision by design:
topic follow-ups ("do the same thing for their pricing") stay follow-ups so a false
fresh-run can't waste ~$0.50 and discard the current session.

**Verified:** `evals/test_router_intent.py` — 13 new tests including the user's literal
phrasing; the live REPL gate is the real proof (run `analyze gusto.com`, then the switch
phrase, confirm a fresh `deel.com` brief is written).

---

## F2 — ScrapeCreators HTTP 402 mid-run (free credits exhausted) · mitigated

**What happened:** During the Phase 6 live Gusto regen, `meta_ads` and `google_ads` both
returned HTTP 402 — the 100 free ScrapeCreators credits had exhausted. Ad claims stopped
flowing; the ad-signal branch (`likely_winner` / `possible_test`) went empty.

**Root cause:** free-tier credit budget is small (100 credits); a handful of full runs +
dev iterations exhaust it. Not a code bug — a resource constraint.

**Fix/mitigation:** (a) the Apify fallback for `meta_ads` and SerpApi fallback for
`google_ads` are wired to fire on a ScrapeCreators `ToolFailure` (DECISIONS §6/§7), so a
402 degrades to a paid fallback rather than crashing; (b) the user rotated a fresh
ScrapeCreators key → ad claims restored. Both fallbacks self-gate on their own keys and
return `empty` (not noise) when they can't corroborate the advertiser.

**Verified:** `evals/test_meta_ads.py` + `evals/test_google_ads.py` cover success / empty /
both-fail / fallback-degradation paths via `ReplayTransport`.

**Tracked:** credit-aware routing (prefer free sources when credits run low) is DECISIONS
§15 item 4 — not yet wired.

---

## F3 — Cache hit rate decays 68% → 9% across a long run · tracked

**What happened:** The cost line showed a 68% cache hit rate on a 2-tool run but 9–15% on
the 8-tool Gusto run and the in-session follow-ups. Prompt caching IS engaged
(`cache_creation` tokens flow on the system prompt + tool schemas), but the cached prefix
shrinks as a fraction of total input as tool-result digests accumulate in the message
thread.

**Root cause:** only the system prompt + tool schemas are cached (`cache_control:
ephemeral`). The growing message thread is not cached, so each turn re-bills a larger
uncached suffix.

**Fix/status:** partially shipped 2026-07-28 — cache breakpoints now use `ttl: "1h"`,
so in-session follow-ups no longer log 0% after a >5-minute reading pause. The dilution
half remains tracked (DECISIONS §15 item 6): a `cache_control` breakpoint on the last
message block would cache the conversation prefix too. Not a correctness bug — the
caching works, the metric just dilutes. Noted honestly in the cost report framing rather
than claimed as a solved problem.

**Verified:** `agent/cost.py::cache_hit_rate` computes the rate from real `usage` fields;
no test asserts a specific rate (it's run-dependent).

---

## F4 — Wayback "today" fetch of JS-rendered pages returns empty · tracked

**What happened:** `wayback_diff` diffs archived snapshots (~90 / ~180 days ago) against
"today." The live `httpx` fetch of a JS-rendered homepage (e.g. `gusto.com`) returns an
empty shell — the hero/headline copy is client-rendered, so the diff has no "today" text
to compare against.

**Root cause:** the "today" side of the diff assumes server-rendered HTML; modern
marketing sites render client-side.

**Fix/status:** tracked (DECISIONS §15 item 5). Mitigation planned: fall back to the
most-recent Wayback snapshot as "today" when the live fetch is empty. The tool already
degrades gracefully to `status="empty"` (never crashes) and the orchestrator notes sparse
snapshots in the brief. Not yet fixed.

**Verified:** `evals/test_wayback_diff.py` covers the empty/sparse-snapshot degrade path.

---

## F5 — `make demo` does not actually replay offline fixtures · tracked (README softened)

**What happened:** The README advertised `make demo` as "Offline demo (replays recorded
data-API fixtures)." In reality `INTEL_MODE=demo` only relaxes key validation; the CLI
still builds a `LiveTransport`, so `make demo` behaves like `make run` and burns live
credits. `ReplayTransport` exists and the eval suite uses it, but it isn't wired into the
CLI entrypoint.

**Root cause:** the demo-mode seam (config key validation) was added without the
transport-switch that would make it real.

**Fix/status:** README claim softened to "runs the live Sonnet/Haiku loop on the golden
competitor; offline fixture replay is a tracked next step." Wiring `ReplayTransport` into
`INTEL_MODE=demo` is the real fix (Phase 5 todo line 84 / Phase 7 todo line 178) — not
done yet, to avoid destabilizing the live path.

**Verified:** the honesty fix is in `README.md`; no code claim of offline replay remains.

---

## F6 — `APIs: $0.00` in the cost line implies measured data-API spend that isn't tracked · fixed

**What happened:** Every run's cost line printed `APIs: $0.00`, suggesting data-API spend
was measured and happened to be zero. In fact `CostAccumulator.api_spend` is never
incremented — no tool reports per-call vendor spend. The $0 figure is accurate only
because the free tiers cost nothing, not because spend was accrued.

**Root cause:** the cost accumulator has an `api_spend` field and a format slot, but no
tool feeds it; paid fallbacks (Apify ~$0.015/run, SerpApi per-search) spend real money that
isn't reflected.

**Fix:** the cost line now tags `APIs: $0.00 (free tier)` when `api_spend` is 0, so the
line doesn't imply measured spend that isn't tracked. (Accruing real per-vendor spend is
DECISIONS §15-class future work.)

**Verified:** `evals/test_cost.py` updated to assert the honest tag.

---

## F7 — Ad `signal` was dead in production despite green unit tests · fixed

**What happened:** After the Phase 6 signal-classification work, the live Gusto brief's
"What's Winning" / "What Looks Like a Test" sections had no deterministic signal data
behind them — 0/26 ad claims classified. `test_build.py` passed because hand-crafted raw
dicts included `last_seen`.

**Root cause:** the live Haiku extractor, told "never invent dates," omits `last_seen` for
active ads (it can't know today's crawl date). So every real ad claim had
`last_seen=None` → the signal branch skipped.

**Fix:** `ledger/build.py` now defaults `last_seen = fetched_at` when `first_seen` is set
and `last_seen` is absent — an observation (the ad is in the active-ads library as of this
crawl), not a guess. After the fix, 26/26 ad claims classified.

**Verified:** live Gusto regen; `evals/test_build.py` + `evals/test_signal.py`.
**Meta-lesson:** deterministic facts belong in Python, never in the model. Logged in
`tasks/lessons.md`.

---

## F8 — Synthesis intermittently returned a blank brief · fixed

**What happened:** The Phase 6 synthesis sometimes wrote an empty brief
(`stop_reason="max_tokens"`, zero text blocks). Flaky because it depended on thinking length.

**Root cause:** Claude Sonnet-5 emits thinking blocks by default; `max_tokens` is the
*combined* thinking + text budget. For long-form synthesis, thinking sometimes consumed
the entire 4096-token cap, starving text output.

**Fix:** prose-only model calls (synthesis, grounding retries, follow-up answer) now pass
`tools=[]` AND `thinking={"type":"disabled"}` — constrain the request to forbid every
shape except the one we want.

**Verified:** live Gusto regen produced the full 6-section brief; `agent/llm.py::create_text`.
**Meta-lesson:** logged in `tasks/lessons.md`.

---

## F9 — Per-turn step cap vs cumulative counter dropped every follow-up tool call · fixed

**What happened:** After an 8-tool initial run, every follow-up tool call was silently
dropped in production. `follow_up` passed `_tool_cycle` a per-turn cap
(`FOLLOWUP_MAX_STEPS=4`), but the guard is `session.steps < max_steps` and `session.steps`
is cumulative — so `8 < 4` was always False.

**Root cause:** a per-phase cap compared against a cumulative counter can never agree after
the first phase consumes any of the total.

**Fix:** the per-turn cap is now passed as `steps_at_entry + max_steps`.

**Verified:** `evals/test_followup_phase7.py` + the live follow-up gate.
**Meta-lesson:** logged in `tasks/lessons.md`.

---

## F10 — Run-again prefix list missed "run this again" · fixed

**What happened:** the router matched "run again for X" but not the user's literal "run
this again for X" (doesn't start with "run again"). The test used "run again for gusto"
(no "this"), so it passed while the real phrasing broke.

**Root cause:** a prefix table only matches the exact phrases you listed; natural speech
has variants you didn't enumerate.

**Fix:** replaced the prefix list with `_RUN_AGAIN_RE`, a regex capturing the *shape*
(`(run|do)\s+(this|it|that)?\s*again`, `start over`, etc.); tests seeded with the user's
literal phrasings.

**Verified:** `evals/test_router_intent.py` (the "natural run-again phrasings" block).
**Meta-lesson:** adversarial verification catches what green pattern-tests don't.

---

## F11 — URL-health classified gated pages (403) as hallucinated · fixed

**What happened:** `check_urls_sync` on the real Gusto/Deel ledgers classified real,
existing pages as HALLUCINATED — `g2.com/products/deel/reviews`,
`gusto.com/product/pricing` returned HTTP 403 (bot-gated) and fell through to the
"not found → check Wayback" path. But 403 means "exists, access denied" — the opposite of
hallucinated.

**Root cause:** classification only special-cased 2xx/3xx as LIVE; 403 fell through with 404.

**Fix:** URL-health now distinguishes by existence-semantics: 2xx/3xx → LIVE;
401/403 → LIVE (gated, not hallucinated); 5xx → UNREACHABLE; 404/410 → DEAD (Wayback
snapshot) or HALLUCINATED (no snapshot). After the fix: 0 hallucinated URLs across both
ledgers (vs. arXiv 2604.03173's 3.5% baseline).

**Verified:** `evals/test_url_health.py` + live ledger run.
**Meta-lesson:** logged in `tasks/lessons.md` (L5).

---

## F12 — Wayback CDX returned the 2007 prior-owner era · fixed

**What happened:** `wayback_diff` once produced "Gusto is a travel reviews and lifestyle
platform" — `gusto.com`'s 2007 prior-owner site, not the payroll company.

**Root cause:** CDX returns snapshots in ascending timestamp order; an unbounded `limit=100`
returned the 100 OLDEST snapshots (the 2007 travel-site era). Two valid 2026 snapshots
existed but were truncated off.

**Fix:** CDX queries are now date-bounded (`from`/`to` = the lookback window) so only
relevant recent snapshots return. The window computation is a static method the test mirrors.

**Verified:** `evals/test_wayback_diff.py`.
**Meta-lesson:** logged in `tasks/lessons.md` (L4).

---

## F13 — Layer-2 judge: placeholder key shadowing + deprecated model + key leak · fixed

**What happened (three issues, all caught by the first live `make eval-judge` run, none by
the stubbed suite):**

1. **401 on OpenAI despite a Gemini key.** The user added `GEMINI_API_KEY` but
   `select_provider` picked OpenAI and 401'd. Cause: a copied `.env.example` left
   `OPENAI_API_KEY=sk-...` (a 6-char placeholder) non-empty, and the provider check was
   naive presence — so the dead OpenAI placeholder shadowed the real Gemini key.
2. **404 on `gemini-2.5-flash`.** After fix 1, Gemini authed but the model 404'd: *"This
   model is no longer available to new users."* The model ID pinned in code had been
   deprecated for new API keys.
3. **API key leaked into the terminal.** The 404 error from `_with_retry` embedded the full
   request URL, and Gemini auth was in the URL as `?key=<full key>` — so the key printed in
   plain text.

**Root cause:** (1) the lessons.md L2 "key is set ≠ key is valid" rule wasn't applied to the
judge's provider selection; (2) a hardcoded model ID drifts as providers deprecate; (3) query-
param auth puts a secret in a place httpx includes in error strings.

**Fix:**
- `select_provider` now filters placeholders via `_looks_like_real_key` (non-empty, no `...`,
  len ≥ 20) — a stale `sk-...` no longer shadows a real Gemini key. (4 new tests.)
- `_GEMINI_MODEL` bumped to `gemini-3.5-flash` (verified live; 2.5-flash is deprecated).
- `_gemini_call` auth moved to the `x-goog-api-key` header; `_with_retry` error messages now
  report status + a short body hint, never the request URL.

**Verified:** `make eval-judge COMPETITOR=gusto.com` and `deel.com` both ran live and wrote
`outputs/{slug}_judge.json` (Gusto 1.00/0.92/0% hall; Deel 0.97/0.84/0% hall).
**Security note:** the Gemini key was printed to the terminal once during diagnosis (before
fix 3) — it should be rotated.

---

## Summary

- **Fixed:** F1, F6, F7, F8, F9, F10, F11, F12, F13.
- **Mitigated:** F2 (fallbacks wired; credit rotation).
- **Tracked (not yet fixed, stated honestly in DECISIONS §15 / README):** F3 (cache prefix),
  F4 (Wayback "today"), F5 (offline demo wiring).

The recurring meta-failure (F1, F7, F9, F10, F13) is that **green stubbed unit tests do not
prove the system works on real model output, real user phrasings, or real provider APIs**.
The standing rule (live end-to-end gate + adversarial verification) is now encoded in the
workflow; the remaining live gate for this pass is the F1 switch-routing REPL run.

## F14 — Body-eligible (0.5) claims hidden in the "Unverified signals" appendix · fixed

**What happened:** the first live BambooHR brief listed 18 items under "Unverified
signals" — a section whose own header says "Confidence < 0.5" — but most sat at exactly
conf 0.5 (press-sourced launches, mission statements). The deterministic Evidence-quality
table on the same page said "Hypothesis-only (confidence < 0.5): 6", so the brief
contradicted itself in plain sight. Caught by reading the live output, not by any test.

**Root cause:** the grounding validator was asymmetric. It quarantined sub-gate claims
out of the body but had no check for the reverse direction — supra-gate claims dumped
into the appendix — so the model could demote body-eligible claims and every eval stayed
green while the flagship "deterministic gate" story was falsified in-brief.

**Fix:** symmetric check in `ledger/grounding.py` (`supragate_in_appendix` on
`GroundingReport`, wired into `_ground_with_retry`'s regenerate feedback), plus a hard
rule in the synthesis prompt (orchestrator_v3 §6). Verified by five new grounding tests.

## F15 — Re-run clobbered the polished Gusto outputs · fixed

**What happened:** a re-run of gusto.com landed in a bad-luck window (Wayback CDX 503
twice, Meta ads empty) and overwrote the shipped 126-claim brief/ledger with a much
thinner one (~1,850 fewer JSON lines). Only git history saved the good artifacts.

**Root cause:** `_write_outputs` overwrote unconditionally — "monitoring loop" behavior
with destroy-on-rerun semantics.

**Fix:** `_archive_prior_outputs` moves the previous brief/intel/canonical files to
`outputs/history/{slug}/{timestamp}/` before every write and prints the location.
Verified by `evals/test_output_archive.py` (archives when priors exist; no history dir
on first run).

## F16 — Follow-up turn: cumulative cost line + leaked meta-commentary · fixed

**What happened (two issues, one live follow-up):** "Dive deeper into their pricing"
answered from the ledger in 22s with zero tool calls, but printed "Run complete: 22s ·
9 tool calls · $0.61" — the SESSION totals dressed up as a per-run line (the turn's real
delta was ~$0.15, also above the README's advertised $0.01–0.06). And the reply opened
with "Since there's no new question in this turn beyond the request to reformat..." —
the forced prose-only answer turn (the anti-regen guard) narrating its own scaffolding
to the user.

**Root cause:** (1) the REPL passed cumulative `session.cost` / `session.steps` into
`format_line` on every turn; (2) the forced final turn's instruction didn't restate the
user's question, so after the tool-cycle had already answered, the model read the bare
"now answer conversationally" as a reformat request and said so.

**Fix:** `CostAccumulator.minus()` + entry snapshots → per-turn "Turn complete" delta
line with a dim session total; `_followup_answer_instruction(question)` restates the
question verbatim and forbids meta-commentary about instructions/formatting/earlier
turns. Cost delta covered by new tests; the prompt fix needs the live gate
(`make eval-live`) — stubs can't catch phrasing regressions (see F-meta in lessons).

## F17 — Re-run diff reported 80 new / 119 removed on an unchanged competitor · fixed

**What happened:** the gusto.com re-run diff printed "80 new, 119 removed, 7 unchanged"
against a ledger captured hours earlier — Gusto's marketing had not changed; the
extractor's phrasing had ("...than competitors" vs "...than rivals"). As "the monitoring
loop", that's noise wearing a signal's clothes.

**Root cause:** `diff_ledgers` matched on exact normalized statements; Haiku extraction
is not phrase-stable across runs.

**Fix:** exact matching first, then a greedy fuzzy pass (token-set Jaccard >= 0.6, each
statement matched once) in `ledger/persist.py`; the summary now reports "(N reworded)".
Verified by two new tests (rewording matches; unrelated statements don't). Residual
honesty note: fuzzy matching reduces phrasing churn, it cannot eliminate it — the README
frames the diff as a "what actually changed" watch, not a guarantee.

## F18 — "No archive evidence" note overstates: wayback claims exist, just not as recent_change · open

**What happened:** both v3-format briefs (Gusto + Deel, 2026-07-15) open "What Changed
Recently" with the honest-disclosure line ("No archive evidence available this run —
wayback_diff did not return snapshot data"), yet the same briefs' Confidence-by-Source
tables show wayback_diff claims present (5 for Deel at conf 0.70). The tool worked; its
claims were categorized `positioning`/`messaging`, not `recent_change`, so the model's
disclosure is directionally right (no archive-backed CHANGE evidence) but literally
wrong ("did not return snapshot data").

**Root cause:** the wayback→recent_change pipeline (cf. F4/F12) — snapshot diffs that
show stable messaging produce positioning claims, and the extractor rarely emits
`recent_change` from wayback text. The prompt's disclosure branch keys on "no
wayback_diff recent_change claims" but the model phrases it as "no wayback data".

**Fix direction (open):** either (a) deterministic: compute the disclosure line in
Python from the ledger (count wayback claims vs wayback recent_change claims) and
inject it, same pattern as the freshness stamp; or (b) prompt: distinguish "wayback
returned nothing" from "wayback shows no change — messaging stable since ~180d ago"
(the latter is itself a finding worth stating). (b) is more useful: "stable" is signal.

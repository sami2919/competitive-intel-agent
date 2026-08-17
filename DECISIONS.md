# DECISIONS.md — Competitive Marketing Intel Agent

Every non-obvious trade-off, with the reasoning. Read this alongside the [README](README.md)
for the quickstart and architecture overview.

## 1. Single orchestrator + deterministic tools (no agent-to-agent chains)

**Decision:** one Claude Sonnet planning brain in a hand-rolled `tool_use` loop,
calling deterministic Python tools. No agent-to-agent handoffs, no multi-agent graph.

**Why:** the "gambler's ruin" argument — each agent turn is probabilistic, and
chained agents compound failure odds. Anthropic's own research-agent writeup measures
multi-agent systems at ~15× the tokens of single-agent, with token usage explaining
~80% of performance variance. We take the orchestrator-worker *shape* but keep it
single-brain + one cheap extraction worker (Haiku), so we get the coverage without the
15× cost or the compounded-failure risk.

**Divergence from prior art:** `open_deep_research`, `deep-competitive-analyst`, and
`gpt-researcher` all use multi-step agent graphs. None of them ship *domain* evals
(open_deep_research does ship generic Deep Research Bench evals — but no grounding
validation, no URL-health checks, no trajectory assertions), and none have ad-library
depth or Wayback diffing — the things that actually differentiate this problem.

## 2. Hand-rolled Anthropic SDK loop, not LangGraph/CrewAI

**Decision:** ~200-line hand-rolled `tool_use` cycle (`agent/loop.py`): plan → act
(call a tool) → observe (a claim *digest*, not raw pages) → re-plan → synthesize.

**Why:** this problem is one planner with tools, not a stateful graph. A framework
would add abstraction without buying anything — and `loop.py` is meant to be the
most-read artifact in the repo, so it must be readable, not buried under framework
plumbing. The loop is the place DRY(E), the step budget, skip-empty, and the one
clarifying question are enforced.

## 3. Model tiering

| Job | Model | Why |
|---|---|---|
| Orchestration + synthesis | `claude-sonnet-5` (fallback `claude-sonnet-4-6`) | judgment per dollar |
| Bulk claim extraction | `claude-haiku-4-5` | 10–20× cheaper; extraction is a constrained task |
| LLM-as-judge evals (Phase 4) | GPT-4o or Gemini | different model family → no self-preference bias |

Extraction is batched (several `### SOURCE:` blocks per Haiku call) with per-claim
`source_url` attribution surviving the batch — so DRY(E) holds: we never re-extract.

*Attribution note:* DRY(E) here expands to **"Don't Repeat Your Embeddings"** — a
principle for making GTM AI agents deterministic and cost-efficient ("compute
redundancy compounds at scale"). We generalize the same principle from embeddings to
extraction: transform once at ingestion, answer every follow-up from the precomputed
ledger. The phrase "do the hard reasoning once" used in this repo is our paraphrase of
that principle.

## 4. Prompt caching (the DRY(E) cost lever)

**Decision:** the orchestrator system prompt and tool schemas are sent as cached blocks
(`cache_control: ephemeral`). Cache hit rate is logged in every run's cost line.

**Why:** the system prompt + tool schemas are static across every turn. Caching them
makes follow-ups ("dig deeper on pricing") read from cache instead of re-billing the
full prefix. First live Gusto run already showed a 41–51% cache hit rate *within a
single run* (the clarifying turn writes the cache; the follow-up reads it).

## 5. Claims ledger + deterministic confidence (no model vibes)

**Decision:** every claim is a `Claim` with `evidence ≥ 1`, a `confidence` in [0,1]
computed by a Python rubric, and a `confidence_trace` explaining the score.

**Rubric:** 2+ independent sources agree → 0.9; single primary (their own site/ads) →
0.7; single secondary (press/review) → 0.5; inferred → 0.3. **D5 ads branch:** ad
claims are capped at 0.7 (performance inferences never hit 0.9). **Gate:** claims <
0.5 go to an "Unverified signals" appendix, never the brief body — "it's better to
skip a prospect than send garbage."

**Why:** determinism + auditability. Confidence is recomputable from inputs (Layer-1
eval re-derives every score and compares), so a reviewer can trust the number without
trusting the model.

## 6. Meta Ad Library — the US-ads gotcha + ScrapeCreators two-step ⭐

**The gotcha (centerpiece):** the *official* Meta Ad Library API returns **nothing**
for US commercial ads — it covers political/issue ads and EU/UK only. Using it would
silently produce "Gusto runs no ads" for every US competitor. This is why we use
ScrapeCreators instead.

**Two-step flow (verified live, not guessed):** a bare keyword `/search/ads` call
matches the *word* "gusto" (Spanish/Italian for "taste") in ad copy across Amazon
México, Nescafé Dolce Gusto, and Bimbo — **not** the advertiser Gusto. So:
1. `GET /search/companies?query=<name>` → resolve the competitor's official page.
   Selection: exact `name` match (case-insensitive); ties broken by `BLUE_VERIFIED`
   preference + financial/B2B `category` + like count. This correctly picks
   `Gusto | Financial Service | BLUE_VERIFIED` and rejects `GUSTO` (Kitchen/cooking),
   `Gusto` (Shopping & Retail), and every Dolce Gusto coffee page.
2. `GET /company/ads?pageId=<page_id>` → that page's ads (`snapshot.body.text` copy,
   `start_date_string` for longevity, `targeted_or_reached_countries` for regions,
   `is_active`).

If the official page can't be resolved, return `status="empty"` rather than ship noisy
keyword ads. Verified: Gusto's resolved page returns 431 ads; first claim is real copy
("Pay your team weekly, or use different pay schedules — infinite payrolls at no extra
cost").

**Apify fallback (wired, not just documented):** if ScrapeCreators returns a
`ToolFailure` — e.g. HTTP 402 when the 100 free credits exhaust — `meta_ads` falls back
to the `apify/facebook-ads-scraper` actor via Apify's sync run endpoint
(`POST /v2/acts/apify~facebook-ads-scraper/run-sync-get-dataset-items?token=<key>`),
searched by `startUrls` = the competitor's Facebook page URL (derived from the domain;
the actor resolves the page ID itself). Two design constraints:
- **Fires on `ToolFailure` only, never on `status="empty"`.** A genuine zero-ads result
  is not re-queried on a paid API — "it's better to skip a prospect than send garbage"
  cuts both ways: don't burn Apify credits second-guessing a legitimate empty.
- **Self-gates on `APIFY_API_KEY`.** The key is NOT in `required_env`, so live mode still
  starts when only ScrapeCreators is keyed; if the key is absent the fallback returns a
  `ToolFailure` and `run()` returns the original ScrapeCreators failure (annotated with
  the Apify outcome) — no wasted round-trip.

The actor's output mirrors ScrapeCreators closely (`snapshot.body.text` copy — identical
path, so `_ad_copy` reuses; `startDateFormatted`, `targetedOrReachedCountries`,
`isActive`), so the generalized field readers + the D5 excerpt format are shared. The
sync endpoint blocks until the actor finishes, so the request carries a 120s
per-request timeout (`HttpRequest.timeout`, honored by `LiveTransport` over the 30s
default) — a transport enhancement any long call can use. Cost: ~$0.75/1000 results × a
`resultsLimit` of 20 ≈ $0.015/fallback. Live-verified dispatcher behavior (ScrapeCreators
402 → fallback triggers → self-gates without a key); success/empty/both-fail paths
covered by `evals/test_meta_ads.py` via `ReplayTransport`.

## 7. Google Ads — ScrapeCreators, region metadata-only

**Decision:** ScrapeCreators Google Ad Transparency endpoints, `x-api-key` auth.

**Gotcha:** region filter is metadata-only; search by advertiser/domain (same shape
lesson as Meta — resolve the advertiser, don't keyword-match).

**SerpApi fallback (wired, not just documented):** if ScrapeCreators returns a
`ToolFailure` (e.g. HTTP 402), `google_ads` falls back to SerpApi's
`google_ads_transparency_center` engine (`https://serpapi.com/search`, `api_key` query
param). Same two design constraints as the meta_ads Apify fallback:
- **Fires on `ToolFailure` only, never on `status="empty"`.**
- **Self-gates on `SERPAPI_API_KEY`** (NOT in `required_env`); no key → returns the
  original ScrapeCreators failure annotated with the SerpApi outcome.

SerpApi has no domain-keyed lookup, so the fallback searches by `text=<domain>` (Google's
"search by advertiser or website name"), which returns creatives across MULTIPLE
advertisers — the same keyword-noise shape as the meta_ads "gusto = taste" gotcha. We
filter to the competitor's own creatives via the list creative's `target_domain` field
(name match as a secondary fallback); if nothing matches, return `empty` rather than ship
another advertiser's ads. Regions need a second per-ad call
(`google_ads_transparency_center_ad_details`), capped at `SERPAPI_MAX_DETAIL_ADS = 5` to
bound SerpApi per-search spend (1 list + 5 details = 6 searches/fallback). The ad_details
engine puts regions under `search_information.regions` (each `{region_name, ...}`). Ad
COPY (headline/description) is NOT exposed by SerpApi — Google renders text into the
creative image and SerpApi captures only the image URL — so text ads degrade to
`[text ad - no copy available]` in the excerpt (metadata + regions, no fabricated copy).
A failed detail call degrades to metadata-only, never breaks the fallback. SerpApi list
creatives use unix-epoch dates, converted to ISO for the shared excerpt format. The 30s
default transport timeout is sufficient (SerpApi search is fast, unlike the Apify sync
actor run). Live-verified end-to-end with a real key: ScrapeCreators 402 → fallback
triggers → real Google ad metadata (creative IDs, format, dates — Gusto creatives running
since 2022-08-25) + regions; copy honestly absent. success/empty/both-fail/detail-
degradation paths covered by `evals/test_google_ads.py` via `ReplayTransport`.

## 8. Exa over Tavily / Brave for news/press

**Decision:** Exa `/search` with `category="news"`, `contents={highlights: true}`,
field-level citations feeding the `Evidence` schema natively.

**Why:** Tavily was acquired by Nebius (roadmap/API stability risk); Brave retired its
free tier. Exa's free tier + neural news index + per-result `publishedDate`/`highlights`
fit the recency-weighted, citable claim flow. We use raw `httpx` through the shared
transport (no `exa-py` SDK) — the D1 "no per-vendor SDKs" rule keeps one retry/timeout
seam for every source.

## 9. Firecrawl credit strategy

**Decision:** `/map` first (1 credit), then `/scrape` ONLY homepage, /pricing, a few
product pages, and recent blog posts. Avoid Stealth mode (5× credits). Avoid `/extract`
(separate token billing) — we do our own extraction with Haiku.

**Budget:** 500 free credits. A Gusto run uses ~13 (1 map + ~12 scrapes). At that rate,
~35 full runs fit in the free tier — well beyond the two-competitor minimum.

## 10. Wayback diff — the only evidence-based "what changed recently"

**Decision:** Wayback CDX API (free, no key) to diff homepage + /pricing hero/headline
copy at ~90 and ~180 days ago vs today.

**Why:** every other "what changed" signal is inferred. Wayback gives *archived
evidence*. Rate limits are unofficial → throttle (sleep between requests) and degrade
gracefully to `status="empty"` when snapshots are sparse. This is a NEVER-cut
differentiator — none of the reference repos do it.

## 11. `ANTHROPIC_BASE_URL` pin (live-wiring gotcha)

**Decision:** `make_client` pins `base_url="https://api.anthropic.com"` by default;
override only via a project-specific `INTEL_ANTHROPIC_BASE_URL`.

**Why:** the Anthropic SDK silently reads `ANTHROPIC_BASE_URL` from the shell. In an
operator environment that runs Claude Code through a proxy (Fireworks/OpenRouter/
LiteLLM), that var is set globally and leaks into `uv run`/`make` subprocesses —
silently repointing the project's API calls at the proxy, which 401s a real Anthropic
key. We hit this live: two valid keys 401'd until the endpoint was pinned.

## 12. Failures are data + replay transport

**Decision:** every tool returns `SourceResult | ToolFailure` — never raises into the
loop. `LiveTransport` does timeout + one retry + backoff; `ReplayTransport` looks up
recorded fixtures by `HttpRequest.key()` (method + url + params + json body — headers
excluded so auth-bearing and auth-less requests hit the same fixture).

**Why:** the orchestrator routes around failures ("Meta Ads is dead after one retry —
skipping per protocol and noting it in the brief") instead of crashing. The replay
seam lets `make demo` and the offline eval suite run with zero data-API keys. Trade-off
hit during the build: replay being auth-agnostic masked missing auth headers until
live-wiring — fixed by tool-declared `required_env` + a live smoke test.

## 13. Evals: three layers, cross-family judge, no external eval platform

**Decision:** Layer-1 deterministic (pytest: schema, evidence≥1, timestamp parsing,
URL health HEAD-checks [LIVE/DEAD/HALLUCINATED, citing arXiv 2604.03173], confidence
recomputation, grounding); Layer-2 cross-family LLM judge (GPT-4o/Gemini via Batch
API — different family to avoid self-preference); Layer-3 trajectory (tool-call trace
assertions + golden set: Gusto→SMB/price-first, Deel→global/EOR).

**Why not Braintrust/LangSmith:** they'd add a managed dependency and a regression-gate
we don't need at this stage. We note in this file that production would add CI
regression gates on top of the local harness.

## 14. ToS — public data only

**Decision:** every source is public (public sites, public ad libraries, public
review pages, Wayback's public archive, public careers pages). We never scrape behind
a login. ScrapeCreators/Firecrawl/Exa are used as authorized intermediaries for
public pages. Any active public litigation involving a competitor is referenced only
via public sources, neutral framing, no speculation.

## 15. What's done vs. next

**Done in this build:**
- **Persist + re-run diff** (the monitoring loop): `outputs/{slug}_intel.json` is loaded
  on re-run and diffed by normalized statement → "N new, M removed, K unchanged" (turns
  the agent from a one-shot into a weekly competitor watch).
- **Haiku clustering → `CanonicalClaim` (D2):** same-assertion claims across independent
  sources merge into the 0.9 corroboration tier — the Gusto run produces 17 canonical
  claims, 12 at 0.9 (e.g. "500,000+ businesses" corroborated across homepage + ads).
  Minimal wiring: canonical claims are saved to `outputs/{slug}_canonical.json`; the
  brief still cites flat `[CLM-xxx]` claims (the validated, grounded flow).
- **URL health:** every cited URL HEAD-checked → LIVE / DEAD / HALLUCINATED (403 = gated
  = LIVE, not hallucinated); 0 hallucinated across the Gusto + Deel ledgers (deep-research
  agents hallucinate 3–13% of cited URLs per arXiv 2604.03173 — we measure ours with the
  same Wayback methodology). `make eval-urls` sweeps every shipped ledger repeatably.
- **Grounding validator wired into the loop** (§5): post-synthesis `check_grounding` with
  up to 2 retries; the Gusto brief's sub-gate-in-body leak was caught and self-corrected.
- **Full `[CAN-xxx]` synthesis citation** (Phase 6 step 4): canonical claims are cited
  directly in the brief and validated by the grounding check.
- **Output-usefulness pass (2026-07-15, research-backed — BLUF/ICD-203, Klue/Crayon
  battlecard anatomy, ad-longevity thresholds):** BLUF `## Verdict` with an action per
  judgment; battlecard-structured first-party relevance (where they win/we win, landmines,
  objection handling, segment-tagged angles); `## Campaign test hypotheses`; deterministic
  estimative-confidence + ad-longevity labels rendered from the rubric; "data as of"
  freshness stamp; wayback-first "What Changed Recently" with an explicit no-archive-
  evidence note. Plus trust fixes from live-run reading (failure_log F14–F17): symmetric
  gate check (body-eligible claims can't hide in the appendix), archive-before-overwrite
  for outputs, fuzzy re-run diff (token-Jaccard, kills phrasing-drift churn), per-turn
  REPL cost lines, follow-up meta-commentary guard.

**Next:**
1. **Layer-2 cross-family judge via Batch API:** GPT-4o/Gemini faithfulness/specificity/
   hallucination scoring on the golden set at batch prices.
3. **URL health as a hard build gate:** today it's a post-run eval; make hallucinated
   URLs block the run.
4. **Credit-aware routing:** when ScrapeCreators/Firecrawl credits run low, prefer free
   sources (Wayback, G2 public pages) + note the degrade.
5. **wayback "today" robustness:** the live httpx fetch of JS-rendered pages (e.g.
   gusto.com) returns empty; fall back to the most-recent Wayback snapshot as "today".
6. **Cache the message prefix:** only system + tools are cached today; add a cache
   breakpoint on the last message block so long 8-tool runs don't dilute the cache-hit
   rate (observed 68% → 9% as the message thread grew).

## 16. Confidence gate + grounding validator + winning score (trustworthiness model)

**The problem this solves:** a brief can read as "impressive" while quietly resting on
low-confidence claims stated as fact. The reviewer's bar is "trustworthy," not just
persuasive. Three deterministic mechanisms enforce the line between high-confidence
evidence, low-confidence inference, and do-not-use.

**1. Confidence gate (`CONFIDENCE_GATE = 0.5`)** — claims below 0.5 are "hypothesis-only."
The rubric (§5) computes confidence in Python; the gate is a hard quarantine, not a
suggestion.

**2. Grounding validator (`ledger/grounding.py`)** — every `[CLM-xxx]`/`[CAN-xxx]` citation
in the brief must map to a real ledger claim (no hallucinated IDs; cf. arXiv 2604.03173's
3–13% hallucinated-URL range for deep-research agents). Sub-gate (conf < 0.5) claims may
appear ONLY in a permitted hypothesis zone: the "Unverified signals" appendix, the "What
Looks Like a Test" section, or "Campaign test hypotheses". Anywhere else (What's Winning,
What Changed Recently, Rippling-relevance) they fail grounding → regenerate (max 2
retries) → flag in-text if still failing. The check is symmetric (failure_log F14): a
body-eligible (conf >= 0.5) claim listed under "Unverified signals" also fails — the
appendix's "confidence < 0.5" header must never be falsified by its own contents.

  *Why "What Looks Like a Test" is a permitted zone:* it is *defined* as `possible_test`
  claims — ads < 90 days old, which score 0.3 (ad base, no longevity boost) → sub-gate by
  construction. Quarantining them out of that section would delete its purpose. The prompt
  requires every bullet there to be framed as a hypothesis ("may be testing..."), so a
  sub-gate citation inside it is an explicit hypothesis, not a strategic signal stated as
  fact. The carve-out is narrow: sub-gate in any other section still fails.

**3. Winning score (`ledger/signal.py::winning_score`, 0–100)** — each canonical claim
carries `winning=NN/100 (corroboration X, persistence Y, recency Z)`, shown in the digest
and required in "What's Winning." Transparent, retunable weights:
  - corroboration 40 — independent source count, saturating at 4 sources.
  - persistence 30 — ad longevity (last_seen − first_seen) or age since first_seen,
    saturating at 365 days; neutral 15 when no date metadata (non-ad canonicals).
  - recency 30 — full within 30 days of the crawl, 2/3 within 90, 1/3 otherwise;
    neutral 15 when no date metadata.

**Why all three are deterministic:** the model translates pre-computed signals into prose;
it never decides what is winning, what is a test, or what passes grounding. A reviewer can
challenge the *weights* (shown in-brief + here), not the *verdict* — and recompute every
score from inputs (Layer-1 eval).

**Self-documenting brief:** two appended deterministic blocks — `## Evidence quality`
(primary owned-site/ad-library vs secondary press/review split + body-eligible count) and
`## How to read this brief` (the winning/test/unverified rules + the grounding guarantee) —
so a reviewer reading the brief alone understands the validation, without needing the repo.

**Strategy vs product launch vs marketing test (the three-way distinction):** a competitive
brief confuses "what really changed" with "what was merely launched" unless the agent names
the difference. The prompt (`orchestrator_v2` §3) and the `## How to read this brief`
appendix both state the rule explicitly so a reviewer sees the methodology in the brief
itself, not just the repo:
- **Strategic shift** — a change in positioning, ICP, pricing, messaging, leadership, or
  funding. It changes WHO the competitor targets or HOW they frame themselves. Lives in
  "What Changed Recently."
- **Product launch** — a new feature inside the existing story (no change to who/why).
  Relegated to a one-line `Supporting note — recent feature launches` at the bottom of
  "What Changed Recently," never interleaved with shifts. A dashboard or a payment-routing
  feature is a product launch; it only earns strategic framing if the agent explains why it
  matters to positioning (and then it is stated as inference).
- **Marketing test** — a new (<90d), single-channel ad variant. Belongs in "What Looks Like
  a Test," not "What Changed Recently."

Two in-brief additions enforce the same line:
- **`## Strategy in plain English`** (a "so what" box under the framing line) — one grounded
  paragraph restating the competitor's current strategy from body-eligible (conf ≥ 0.5)
  claims only. Synthesis, not new evidence; every assertion traces to a cited claim.
- **"Why it likely persists"** — each "What's Winning" bullet ends with an inferred
  business reason (trust, simplicity, conversion-friction reduction). Tagged
  `inferred, not measured` so interpretation never reads as an evidence-backed conclusion.

**Segment moves and press-driven shifts:** each ICP/segment shift is labeled **new ICP**
vs **extension of an existing motion** (e.g. Gusto's Solo line is classified as one or the
other, not left ambiguous). A strategic shift resting on press/secondary sources or
inference — no durable owned-site or ad evidence yet — is framed as "appears to /
directionally" with a one-line note on why it still qualifies as strategic (the test: does
it change positioning/identity, not just add a feature?). This keeps directionally-right
but medium-confidence shifts (e.g. AI-native repositioning) honest without burying them.

**Confidence scoring — kept, not expanded:** the `winning=NN/100` score and the 0.5
confidence gate are decision inputs the agent actually uses (winning score → "What's
Winning"; gate → sub-gate quarantine), so they stay. The reviewer's note — "prioritize
clarity over extra scoring complexity" — is handled by not adding new scoring and by
tightening narrative consistency (the three-way distinction + inference tags), not by
simplifying the rubric. The appended `## Confidence by Source` / `## Evidence quality`
tables remain as-is.

## 2026-07-27 — linkedin_posts (post-submission, pre-walkthrough)

Closed the self-named LinkedIn coverage gap's organic half: `tools/linkedin_posts.py` via
the Apify Store actor `harvestapi/linkedin-company-posts` (public posts, no login —
consistent with the public-data-only ToS stance; pay-per-event ~$0.001/query). Probe
lesson worth keeping: the obvious first-pick actor (automation-lab's) runs SUCCEEDED yet
returns 0 items even for its own example input — a silent-zero failure mode that green
tests would never catch; the live probe caught it in minutes. Chosen over Adyntel (paid
vendor, new auth surface) and ScrapeCreators (Phase 0 probe: no LinkedIn endpoints, all
404). One file + one fixture test through the existing BaseTool/transport seam — the
adapter-cost claim in DECISIONS.md, demonstrated rather than asserted. LinkedIn *ads*
remain a named gap; same seam, pending a vendor probe.

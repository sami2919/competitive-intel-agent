# Competitive Marketing Intel Agent

A conversational CLI agent that takes a competitor domain (e.g. `gusto.com`) and produces
a structured, evidence-grounded analysis of its **public marketing strategy and positioning** —
messaging themes, pricing, ad creative, recent changes, and campaign-ready angles for
your own company. Every claim is cited, confidence-scored by a deterministic rubric, and
persisted to a claims ledger.

> Architecture follows a "gambler's ruin" + DRY(E) philosophy: **one orchestrator,
> deterministic tools, no agent-to-agent chains** — extract once into a ledger,
> synthesize from the ledger, cache aggressively. See [DECISIONS.md](DECISIONS.md)
> for every trade-off.

## 60-second quickstart

```bash
# 1. Install uv (+ Python 3.12+). No install step needed — every make target wraps
#    `uv run --with-requirements requirements.txt`, which resolves deps on first run.
#    Optional pre-warm / sanity check:
uv run --with-requirements requirements.txt python -c "import anthropic; print('deps ok')"

# 2. Add API keys (.env from .env.example)
cp .env.example .env   # fill ANTHROPIC_API_KEY + the data-API keys

# 3. Run a competitor (live, one-shot)
make run COMPETITOR=gusto.com

# 3b. Or go interactive — takes a company NAME or domain, asks its clarifying
#     question for real, and handles follow-ups on the live session:
make run
#   › analyze Bamboo HR          → Resolved 'Bamboo HR' → bamboohr.com — proceed? [Y/n]
#   › dig deeper on their pricing   (follow-up: reuses the cached thread + ledger)
#   › analyze deel.com              (fresh session)

# 4. Demo — runs the live Sonnet/Haiku loop on the golden competitor
#    (INTEL_MODE=demo relaxes data-API key validation but the CLI still uses the live
#    transport, so this is NOT a fully offline replay). Offline fixture replay via
#    ReplayTransport is a tracked next step — see DECISIONS §15 / evals/failure_log.md F5.
#    For a fully offline, free run, use `make eval`.
make demo

# 5. Eval suite (must stay green)
make eval
```

Every run ends with a cost/latency line (real observed Gusto run):
```
Run complete: 320s · 8 tool calls · $0.42 (cache hit rate 15%)
  Sonnet: $0.30 · Haiku: $0.12 · APIs: $0.00 (free tier)
```

## Outputs

- `outputs/{competitor}_brief.md` — the competitive brief, structured for a campaign
  writer (BLUF): a **Verdict** (3-5 key judgments, each with a "→ For Rippling:" action),
  strategy in plain English, what's winning / what looks like a test (with deterministic
  confidence + ad-longevity labels), wayback-first recent changes, a **battlecard**
  (where they win / we win, landmines, objection handling, segment-tagged campaign
  angles), and ranked **campaign test hypotheses** — all with inline `[CLM-xxx]` /
  `[CAN-xxx]` citations and a "data as of" freshness stamp. Prior versions are archived
  to `outputs/history/` on every re-run.
- `outputs/{competitor}_intel.json` — the serialized claims ledger (every claim with
  evidence, deterministic confidence + trace, extractor model/prompt version).
- `outputs/{competitor}_canonical.json` — cross-source-corroborated canonical claims (the
  0.9 corroboration tier) with deterministic `winning=NN/100` scores.
- `outputs/{competitor}_judge.json` — Layer-2 cross-family judge report (GPT-4o-mini or
  Gemini 2.5 Flash scoring faithfulness/specificity/hallucination + a brief rubric).
  Produced by `make eval-judge COMPETITOR=<slug>`; needs `OPENAI_API_KEY` or `GEMINI_API_KEY`.

## Architecture

```
User (CLI, rich rendering)
  ⇄ Orchestrator: Claude Sonnet (hand-rolled tool_use loop, ~200 lines)
       plan → act (call a tool) → observe (a claim DIGEST, not raw pages) → re-plan → synthesize
       │
       ├── 9 TOOLS (deterministic Python, Pydantic I/O, typed failures, shared transport):
       │     crawl_site, meta_ads, google_ads, wayback_diff, social_posts,
       │     news_press, jobs_signals, g2_reviews, linkedin_posts
       ├── EXTRACTOR: Claude Haiku (batched) — page/ad text → list[Claim]
       └── CLAIMS LEDGER (Pydantic, frozen, deterministic confidence rubric)
              → grounded synthesis (brief) → exports → evals → cost report
```

One planning brain, deterministic tools, failures as data. The message thread carries
**claim digests** (not raw pages, not the full ledger) so token usage stays flat as the
ledger grows (DRY(E)). The full ledger loads at synthesis only.

### Tools

| Tool | Source | Key | What it extracts |
|---|---|---|---|
| `crawl_site` | Firecrawl (`/map`→`/scrape`) | `FIRECRAWL_API_KEY` | homepage, /pricing, product + blog positioning/messaging |
| `meta_ads` | ScrapeCreators (two-step) | `SCRAPECREATORS_API_KEY` | active Meta ad copy, start dates (longevity), regions |
| `google_ads` | ScrapeCreators (two-step) | `SCRAPECREATORS_API_KEY` | Google ad copy, first/last shown, regions |
| `wayback_diff` ⭐ | Wayback CDX (free) | — | homepage/pricing messaging ~90 & ~180 days ago vs today |
| `news_press` | Exa (`/search`, news) | `EXA_API_KEY` | recent press: launches, funding, exec moves, positioning shifts |
| `jobs_signals` | Firecrawl (careers page) | `FIRECRAWL_API_KEY` | marketing/sales/growth hires = ICP/segment signals |
| `g2_reviews` | Firecrawl (G2 public page) | `FIRECRAWL_API_KEY` | review ratings + complaint themes = positioning gaps |
| `social_posts` | Firecrawl (company blog) | `FIRECRAWL_API_KEY` | posting cadence + content pillars/launch themes |
| `linkedin_posts` | Apify (LinkedIn company-posts actor) | `APIFY_API_TOKEN` | recent public company posts — social themes, launch signals, engagement |

Every tool returns `SourceResult | ToolFailure` — failures are DATA the orchestrator routes
around, never exceptions. Each routes through one shared transport (timeout → one retry +
backoff → typed failure), with per-vendor auth headers. `ReplayTransport` lets the eval
suite run offline with zero data-API keys (`make demo` still uses the live transport —
see the quickstart note; wiring demo to ReplayTransport is DECISIONS §15).

### Model tiering

| Job | Model | Why |
|---|---|---|
| Orchestration + synthesis | `claude-sonnet-5` | judgment per dollar |
| Bulk claim extraction | `claude-haiku-4-5` | 10–20× cheaper; constrained task |
| LLM-as-judge evals (Layer 2) | GPT-4o or Gemini | cross-family → no self-preference bias |

Prompt caching: the system prompt + tool schemas are sent as cached blocks; cache hit rate
is logged per run.

## The claims ledger (the core)

```python
Claim(id, competitor, category, statement, evidence: list[Evidence] ≥1,
      confidence, confidence_trace, extracted_by)
```

**Confidence is deterministic, computed in Python — never model vibes:**
- 2+ independent sources agree → **0.9**
- single primary (their own site/ads) → **0.7**
- single secondary (press/review) → **0.5**
- inferred → **0.3**
- Ad-performance inferences: capped at **0.7** (public data can't prove performance)

**Gate:** claims < 0.5 go to an "Unverified signals" appendix, never the brief body —
*"it's better to skip a prospect than send garbage."* The confidence trace stores *why*
each score is what it is, for auditability.

## Eval system (three layers)

- **Layer 1 — deterministic (pytest, `make eval`):** JSON schema validity; every claim has
  ≥1 evidence; timestamps parse; **URL health** (HEAD-check every cited URL → LIVE / DEAD
  (404 + Wayback snapshot) / HALLUCINATED (404 + no snapshot) — citing arXiv 2604.03173);
  confidence recomputation; **grounding** (every brief `[CLM-xxx]`/`[CAN-xxx]` maps to a
  real claim; no sub-gate claims in the body).
- **Layer 2 — cross-family LLM judge (`make eval-judge`, `evals/judge.py`):** a DIFFERENT
  model family — GPT-4o-mini (or Gemini 2.5 Flash if `GEMINI_API_KEY` is set instead) —
  judges a stratified sample of claims for **faithfulness** (evidence supports statement),
  **specificity** (concrete vs vague), and **hallucination** (cited evidence absent/fabricated),
  plus a brief rubric (Rippling-relevance, recency-realness, usefulness). Different family
  on purpose, so faithfulness isn't judged by the model that wrote the claims (no
  self-preference bias). Direct calls (not the Batch API — that's the production note in
  DECISIONS §15). Writes `outputs/{slug}_judge.json`; skips cleanly with no judge key.
- **Layer 3 — trajectory + golden set:** tool-call trace assertions (skip-empty, step
  budget, one clarifying Q, no loops); golden set — Gusto MUST surface SMB/price-first,
  Deel MUST surface global/EOR, BambooHR MUST surface HR-core. Real failure modes caught
  by reading outputs are logged in `evals/failure_log.md`.

Production would add CI regression gates (Braintrust/LangSmith) on top of the local harness.

## Cost & latency

Observed live: a Gusto run is ~**$0.42** (320s · 8 tool calls · Sonnet $0.30 + Haiku $0.12, data
APIs in free tier); a fresh in-session competitor run (e.g. Workday) ran ~**$1.17** (12 tools);
follow-ups reuse the cached ledger for ~**$0.01–0.15** (scales with ledger size and cache-hit
rate; the REPL prints the per-turn delta plus the session total). A ~$30 development
budget held comfortably across two competitors + dev iterations + the judge.
Per-competitor cost varies with tool count and cache-hit rate (9–15% on long 8-tool
runs — only the system prompt + tool schemas are cached today; caching the message
prefix is DECISIONS §15 item 6).

Rough monitoring math at the Gusto run rate: weekly runs of 20 competitors ≈ **$8/week
in LLM** (~$35/month) plus data-API credits — monitoring at scale needs paid Firecrawl /
ScrapeCreators / Exa plans beyond the free tiers. Cost discipline is framed via DRY(E)
reasoning (extract once, cache the prefix, carry digests not raw pages) — not
nickel-counting.

## CRO agent (`make cro`)

A second agent on the same substrate: it turns the competitor ledger this tool already
produced into gated landing-page test variants.

```bash
make cro PAGE=https://www.rippling.com/payroll COMPETITOR=gusto.com SESSIONS=100000
```

Pipeline — one Sonnet call per hypothesis, everything else deterministic Python:

```
page URL ──> page_snapshot (Firecrawl, shared transport) ──> control arm
{competitor}_canonical.json ──> ranked hypotheses (cross_channel_winner first)
                                    │
                          max_runnable_variants()  ← caps generation BEFORE any tokens
                                    │
                          generate (Sonnet, variant_gen_v1)
                                    │
   compliance gate ──> scoring rubric ──> test plan ──> variants.md + .json + optimizely.json
```

**Two categorical rejects, never score penalties:**
- `unsourced_claim` — a comparative or factual assertion with no ledger claim ID and no
  *verified* first-party fact behind it. `data/rippling_facts.yaml` entries default to
  `verified: false`, which behaves exactly like unsourced: an allowlist that trusts its
  own unverified entries is not an allowlist.
- `multivariate` — the variant changed more than one element, so its result cannot be
  attributed to a single variable.

**Specificity is a positive test, not a blocklist.** It counts concrete referents
(numerals, named products, jurisdictions) rather than banning phrases — banning
"seamless" just yields "frictionless", and "Better payroll for growing teams" contains
no banned phrase while asserting nothing.

**Experiment sizing is real math** (`cro/testplan.py`): a two-proportion z-test decides
how many arms the page's traffic can actually resolve inside 28 days. Six variants on a
400-sessions/week page is not a test, and the run refuses to spend tokens on one.

Outputs land in `outputs/cro/{page-slug}_variants.{md,json}` + `_optimizely.json`.

## Repo structure

```
competitive-intel-agent/
├── README.md            ← this file
├── DECISIONS.md         ← every trade-off (single-orchestrator, Meta US-ads gotcha, Exa-over-Tavily, …)
├── agent/               ← loop.py, prompts/ (versioned), extractor.py, cost.py, llm.py, config.py
├── tools/               ← one module per source + _base/_transport/_auth/_util
├── ledger/              ← models.py, confidence.py (rubric), build.py, grounding.py
├── evals/               ← test_* per tool + deterministic/trajectory/golden + url_health + judge.py + failure_log.md
├── outputs/             ← {competitor}_brief.md + _intel.json + _canonical.json + _judge.json
└── Makefile             ← make run / demo / eval / eval-live / eval-judge / eval-urls
```

## Key design decisions (see [DECISIONS.md](DECISIONS.md))

- **Single orchestrator + deterministic tools** (no agent chains) — multi-agent uses ~15×
  tokens (Anthropic research) with compounded failure odds.
- **Hand-rolled Anthropic SDK loop**, not LangGraph/CrewAI — `loop.py` is meant to be the
  most readable artifact in the repo; a framework would bury it.
- **Meta Ad Library US-ads gotcha** — the official API returns nothing for US commercial
  ads; we use ScrapeCreators' **two-step** flow (resolve the official page by name +
  `BLUE_VERIFIED`, then fetch *that page's* ads) so a keyword search for "Gusto" doesn't
  ship Amazon México / Dolce Gusto coffee ads as the competitor's.
- **Exa over Tavily/Brave** for news — Tavily acquired by Nebius, Brave retired free tier.
- **Wayback diff** — the only evidence-based "what changed recently" source (date-bounded
  CDX query, or you get the domain's 2007 prior-owner era).
- **Public data only** — never behind a login. Active public litigation involving a
  competitor is referenced via public sources only, neutral framing, no speculation.

## Status

8 live tools; deterministic confidence rubric (D5 ads cap, sub-gate, **0.9 corroboration
tier via D2 clustering**); self-grounding loop (§5 validator with retry, sub-gate
quarantine, deterministic `winning=NN/100` scores, symmetric appendix check); URL-health
(0 hallucinated URLs across the shipped ledgers — repeatable via `make eval-urls`; the
published deep-research baseline is 3–13% per arXiv 2604.03173); ledger persist +
fuzzy re-run diff (exact + token-Jaccard matching, so extractor rewording doesn't read
as churn — a "what actually changed" watch, with prior outputs archived to
`outputs/history/`); interactive REPL
with clarifying-Q pause, ledger-backed follow-ups (~$0.01 vs full regen), and mid-session
competitor switching; 3-layer evals + golden set.

**Shipped competitor briefs:** Gusto and Deel — `outputs/{gusto,deel}.com_brief.md` +
`_intel.json` + `_canonical.json` + `_judge.json` for each. Both grounded, cited, and
cross-family-judged (Gemini 3.5 Flash: Gusto faithfulness 1.00 / specificity 0.92 / 0%
hallucination; Deel 0.97 / 0.84 / 0%). The Deel golden test passes (surfaces global/EOR
positioning); BambooHR golden is scaffolded (skip-gated).

**Test suite:** `make eval` — 264 tests green, 1 skipped (the live follow-up gate, which
needs `make eval-live`). Real failure modes caught by reading outputs are in
`evals/failure_log.md`.

**Layer-2 judge:** implemented and run (`make eval-judge`, OpenAI or Gemini) —
`outputs/{slug}_judge.json` exists for both shipped competitors.

See [DECISIONS.md](DECISIONS.md) for every trade-off and remaining next steps (cache-prefix
caching, Wayback "today" robustness, offline demo wiring, Batch-API judge).

# CLAUDE.md — Build Spec: Competitive Marketing Intel Agent

You are building a production-quality competitive marketing intelligence agent.
Follow the phases in order, keep every phase shippable, and never break the
acceptance criteria of a completed phase.

---

## 1. What we're building

A **conversational CLI agent** that takes a competitor name/domain (e.g. `gusto.com`) and
produces a structured analysis of their **public marketing strategy and positioning**:

- **Output 1:** `outputs/{competitor}_brief.md` — messaging angles/themes, product positioning,
  what's changed recently, and positioning gaps/opportunities for your own company's marketing.
- **Output 2:** `outputs/{competitor}_intel.json` — the serialized claims ledger
  (sources, extracted claims, confidence levels, timestamps).

Must work for ANY competitor in the configured first-party company's space (Gusto, Deel,
BambooHR, Justworks, Paylocity, ADP, Remote, TriNet...). Nothing hardcoded to one company.
Public data only — never scrape behind logins.

## 2. Design philosophy (encode this in code, not just docs)

- "Gambler's ruin": each agent turn is probabilistic; chained agents compound failure odds.
  → **One orchestrator, deterministic tools. No agent-to-agent chains.**
- DRY(E): "do the hard reasoning once, at ingestion" — every question should not re-pull,
  re-extract, re-burn tokens. → **Extract claims once into a ledger; synthesis reads the
  ledger, never raw pages twice. Use prompt caching aggressively.**
- Determinism + auditability: separate "what to do" (LLM) from "how to format/score it"
  (deterministic code). → **Confidence is computed by rubric in Python, never by model vibes,
  and every score stores its trace.**

Anthropic's own research-agent writeup justifies the architecture: multi-agent systems use
~15x more tokens; token usage explains ~80% of performance variance. We take the
orchestrator-worker pattern but keep it single-brain + cheap extraction worker for cost.

## 3. Architecture

```
User (CLI chat, rich rendering)
  ⇄ Orchestrator: Claude Sonnet (hand-rolled tool_use loop, ~150-250 lines)
       plans → calls tools → observes → re-plans → asks clarifying Qs → synthesizes
       │
       ├── TOOLS (deterministic Python, Pydantic I/O, typed failures):
       │     meta_ads, google_ads, crawl_site, wayback_diff, social_posts,
       │     news_press, jobs_signals, g2_reviews
       ├── EXTRACTOR (Claude Haiku): page/ad text → list[Claim] (constrained extraction)
       └── CLAIMS LEDGER (Pydantic, persisted per competitor)
              → synthesis (brief) → grounding validator → exports → evals → cost report
```

### Tech stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ |
| LLM SDK | `anthropic` SDK, hand-rolled tool_use loop (~150-250 lines) |
| Orchestration + synthesis | claude-sonnet-4-6 |
| Bulk extraction worker | claude-haiku-4-5 |
| LLM-as-judge evals | GPT-4o / Gemini (cross-family, via Batch API) |
| Data models | Pydantic v2 |
| Web crawl | Firecrawl (`/map` then `/scrape`) |
| Ad intelligence | ScrapeCreators (Meta + Google Ad Library), Apify fallback |
| News / press | Exa (or Linkup) |
| History diff | Wayback CDX API (free, no key) |
| Reviews | G2 / Capterra public pages |
| CLI rendering | `rich` |
| Tests / evals | pytest + small custom harness (DeepEval optional) |
| Deps / env | `uv` + `requirements.txt`, `python-dotenv` |

**Framework decision (record in DECISIONS.md):** hand-rolled loop on the Anthropic SDK.
NOT LangGraph/CrewAI — this problem is one planner with tools, not a stateful graph.
Reference and diverge from: langchain-ai/open_deep_research, ALucek/deep-competitive-analyst,
assafelovic/gpt-researcher (none of them do evals, ad-library depth, or Wayback diffing).

**Model tiering:**
| Job | Model | Why |
|---|---|---|
| Orchestration + synthesis | claude-sonnet-4-6 | judgment-per-dollar |
| Bulk claim extraction | claude-haiku-4-5 | 10-20x cheaper, constrained task |
| LLM-as-judge evals | GPT-4o or Gemini | different family → no self-preference bias |

**Anthropic API features to use:** prompt caching (static system prompt + tool schemas +
fetched documents in cached prefix; follow-ups like "dig deeper on pricing" become cheap),
and Batch API for golden-set eval runs. Log cache hit rates into the cost report.

## 4. Data layer — tools, gotchas, fallback chains

Every tool returns `SourceResult(source, url, fetched_at, raw_excerpt, status)` or
`ToolFailure(reason, suggestion)`. Failures are DATA the orchestrator routes around —
never uncaught exceptions. Each tool gets a unit test with a mocked response AND a
recorded-fixture test.

1. **meta_ads** — ScrapeCreators `/v1/facebook/adLibrary/search/companies` → `/search/ads`
   (fallback: Apify actor). **CRITICAL GOTCHA:** the official Meta Ad Library API returns
   NOTHING for US commercial ads (political/issue + EU/UK only) — this is why we don't use
   it; explain in DECISIONS.md. On zero ads: agent surfaces it conversationally
   ("No active Meta ads found — check LinkedIn Ad Library instead?").
   Extract: hooks/angles, offers, CTAs, products pushed, SMB vs enterprise language,
   ad longevity (long-running = winning).
2. **google_ads** — ScrapeCreators Google Ad Transparency endpoints (fallback: SerpApi).
   GOTCHA: region filter is metadata-only; search by advertiser/domain.
3. **crawl_site** — Firecrawl: `/map` first (1 credit), then scrape ONLY homepage, /pricing,
   3-5 product pages, 5-10 recent blog posts. GOTCHA: Stealth mode = 5x credits; avoid
   `/extract` (separate token billing) — we do our own extraction with Haiku.
4. **wayback_diff** ⭐ — Wayback CDX API (free, no key): snapshots of homepage + pricing
   from ~90 and ~180 days ago; diff hero/headline/nav copy vs today. This is the ONLY
   evidence-based answer to "what changed recently." Rate limits are unofficial — throttle,
   and degrade gracefully to press-based evidence if snapshots are sparse.
5. **social_posts** — LinkedIn Ad Library (public, no login; no official API — use
   ScrapeCreators/Apify) + company blog/YouTube RSS. Cadence, pillars, launch themes.
6. **news_press** — Exa (or Linkup) search with date filters; field-level citations feed
   the Claim schema natively. (Chosen over Tavily post-Nebius-acquisition and Brave
   post-free-tier-retirement; note this in DECISIONS.md.)
7. **jobs_signals** — public careers page via Firecrawl; marketing/sales hires = ICP signals.
8. **g2_reviews** — public G2/Capterra pages only; complaints = positioning gaps for the
   first-party company.

Budget: ScrapeCreators 100 free credits, Firecrawl 500 free credits, Exa/Linkup free tier.
Total spend target ≤ $30. Get all API keys in Phase 0.

## 5. Claims ledger (the core)

```python
class Evidence(BaseModel):
    source_url: str
    excerpt: str
    fetched_at: datetime

class Claim(BaseModel):
    id: str
    competitor: str
    category: Literal["messaging","positioning","pricing","ads_paid_social",
                      "ads_search","recent_change","icp_targeting","social_content"]
    statement: str
    evidence: list[Evidence]        # >= 1, enforced
    confidence: float               # DETERMINISTIC rubric, computed in Python
    confidence_trace: str           # why this score — auditability
    extracted_by: str               # model + prompt version
```

**Confidence rubric (deterministic):** 2+ independent sources agree → 0.9;
single primary source (their own site/ads) → 0.7; single secondary (press/review) → 0.5;
inferred → 0.3. **Gate:** claims < 0.5 go to an "Unverified signals" appendix, never the
brief body. Same principle as: "it's better to skip a prospect than send garbage."

**Grounding validator:** the synthesis prompt may only cite claim IDs; a post-generation
check maps every factual sentence in the brief to a claim ID or regenerates (max 2 retries,
then flag). Ledger persists to disk per competitor; re-runs diff against the prior ledger
and report what's new.

## 6. Conversational loop requirements

- `analyze gusto.com` → agent proposes a plan + asks ONE clarifying question
  (SMB vs enterprise focus?) before burning tokens.
- Streams one-line progress ("✓ 47 active Meta ads — clustering themes...").
- **Step budget:** hard cap ~35 tool calls/run; skip-empty-source logic (a source returning
  nothing is noted and skipped, never retried more than once).
- Follow-ups reuse session state: "dig deeper on their pricing" re-enters the loop with the
  existing ledger; "run this again for Deel" starts fresh.
- Use `rich` for rendering. CLI only — no web UI.

## 7. Eval system (three layers)

**Layer 1 — deterministic (pytest, `make eval`):**
- JSON schema validity; every claim has ≥1 evidence; timestamps parse.
- **URL health check ⭐:** HEAD-request every cited URL → LIVE / DEAD (404 but Wayback
  snapshot exists) / LIKELY_HALLUCINATED (404, no snapshot). Cite arXiv 2604.03173 in docs
  (OpenAI Deep Research: 3.5% hallucinated + 10.1% non-resolving URLs — we measure ours).
- Confidence recomputation: recompute every score from the rubric inputs and compare.
- Grounding: every brief sentence maps to a claim ID; no sub-gate claims in the body.

**Layer 2 — cross-family LLM judge (GPT-4o/Gemini via Batch API):**
- Sampled claims: faithfulness (does evidence support statement?), specificity, hallucination.
- Brief rubric: useful to a marketer at the first-party company? Is the relevance
  framing insightful? Recency real?

**Layer 3 — trajectory evals:**
- Assert on tool-call traces: skipped empty sources, stayed under step budget, asked a
  clarifying question on ambiguous input, no loops.
- Golden set: Gusto, Deel, BambooHR with property-based checks
  (Deel run MUST surface global/EOR positioning; Gusto run MUST surface SMB/price-first).

**Human layer:** maintain `evals/failure_log.md` — real failure modes caught by reading
outputs, and the fix. Populate it honestly during Phase 4.

Tooling: pytest + small custom harness. DeepEval optional for judge metrics. Do NOT bolt on
Braintrust/LangSmith — note in DECISIONS.md that production would add CI regression gates.

## 8. Cost & latency accounting

Instrument the loop. Every run ends with:
```
Run complete: 4m 12s · 31 tool calls · $0.47 (cache hit rate 62%)
  Sonnet: $0.31 · Haiku: $0.09 · APIs: $0.07
```
README line: "at ~$X/competitor, weekly monitoring of 20 competitors ≈ $Y/month."
Frame cost discipline via the DRY(E) principle ("do the hard reasoning once, at
ingestion") rather than "count the nickels" framing.

## 9. Repo structure

```
competitive-intel-agent/
├── README.md          # 60-sec quickstart, architecture diagram, demo GIF, cost math
├── DECISIONS.md       # ⭐ all trade-offs: single-orchestrator (cite Anthropic 15x tokens),
│                      #   no LangGraph, Meta API US-ads gotcha, Exa-over-Tavily rationale,
│                      #   repos studied + divergences, Firecrawl credit strategy,
│                      #   ToS note (public data only), "next 2 weeks" roadmap
├── agent/             # loop.py, prompts/ (versioned), session.py, cost.py
├── tools/             # one module per source, Pydantic I/O, typed failures
├── ledger/            # models.py, confidence.py (rubric), grounding.py
├── evals/             # test_deterministic.py, judge.py, trajectory.py, golden/,
│                      # failure_log.md
├── outputs/           # gusto_brief.md + gusto_intel.json, deel_brief.md + deel_intel.json
├── tasks/             # todo.md, lessons.md (dev-process tracking — not shipped)
├── Makefile           # make demo / make eval / make run COMPETITOR=gusto.com
└── .env.example, requirements.txt
```

## 10. First-party relevance synthesis context (seed the prompt with this)

- Acme (first-party, example) = compound platform (HR+IT+Finance, 30+ products), scales
  SMB→enterprise, IT Cloud (MDM/SSO) has no Gusto equivalent, modular pricing from
  ~$8/user/mo.
- Gusto = simplicity + transparent flat pricing, US small business; counter-markets on
  "no surprise fees"; soft spots: multi-state complexity, scale ceiling, thin international.
- Deel = global-first/EOR, aggressive paid presence; soft spots: US-domestic depth,
  EOR pricing criticism. NOTE: when a competitor is party to active public litigation,
  cite it via public sources only, neutral framing, never speculate — this is a general
  rule, not specific to any one competitor.
- Demand campaign-ready angles, e.g.: "Gusto runs price-first SMB ads → counter-position
  'the platform you won't outgrow' for the 30-200 employee migration segment."

## 11. Build phases (each ends shippable)

**Phase 0 (hour 0-1):** repo scaffold, .env, ALL API keys acquired and smoke-tested,
Makefile, Pydantic models. ✅ `make run` prints hello-loop.

**Phase 1 (day 1):** orchestrator tool_use loop with streaming + prompt caching;
crawl_site + meta_ads tools end-to-end; Haiku extractor; ledger writes.
✅ First ugly Gusto brief exists.

**Phase 2 (day 2 AM):** google_ads, wayback_diff, news_press, jobs_signals, g2_reviews,
social_posts; confidence rubric + gate; grounding validator.
✅ Gusto brief is grounded, gated, cited.

**Phase 3 (day 2 PM):** conversational layer (clarifying Qs, follow-ups, session persistence,
step budget, skip-empty logic); Deel run. ✅ Both briefs exist; follow-ups work.

**Phase 4 (day 3):** full eval suite (all 3 layers + URL health + golden set); populate
failure_log.md by actually reading outputs; cost instrumentation; DECISIONS.md + README +
diagram; clean final runs; record Loom. ✅ `make eval` green; ship email to Will + Megan.

**Cut order if time bites:** g2_reviews → jobs_signals → re-run diffing → social_posts.
**NEVER cut:** evals, claims ledger, wayback_diff, two competitors, cost report, DECISIONS.md.

## 12. Working rules for Claude Code

- Write tests alongside each tool, not at the end. `make eval` must stay green.
- Prompts live in versioned files under `agent/prompts/`, never inline strings.
- Every external call: timeout, one retry with backoff, then typed ToolFailure.
- Record fixtures for API responses so evals run offline and free.
- Keep the orchestrator loop readable — it should be the most readable artifact in the repo.
- When in doubt, choose the option that is more deterministic, more auditable, or cheaper —
  in that order.

### Workflow orchestration (dev process — does NOT override the product's no-agent-chain rule)

1. **Plan mode default** — Enter plan mode for any non-trivial task (3+ steps or architectural
   decisions). If something goes sideways, STOP and re-plan immediately; don't keep pushing.
   Use plan mode for verification steps, not just building. Write detailed specs upfront to
   reduce ambiguity.
2. **Subagent strategy** — Use Claude Code subagents liberally to keep the main context clean:
   offload research, exploration, and parallel analysis; one task per subagent. NOTE: this is
   about *development-time* subagents. It does NOT contradict section 2's product rule of one
   orchestrator + deterministic tools + no agent-to-agent chains at runtime. Those are
   different layers — don't conflate them.
3. **Self-improvement loop** — After ANY correction from the user, record the pattern in
   `tasks/lessons.md` and write a rule that prevents the same mistake. Review lessons at
   session start. (Product failure modes go in `evals/failure_log.md`; dev-process lessons
   go in `tasks/lessons.md`.)
4. **Verification before done** — Never mark a task complete without proving it works. Run
   tests, check logs, demonstrate correctness. Ask: "Would a staff engineer approve this?"
   Diff behavior against the prior output when relevant.
5. **Demand elegance (balanced)** — For non-trivial changes, pause and ask "is there a more
   elegant way?" If a fix feels hacky, reconsider with full context. Skip this for simple,
   obvious fixes — don't over-engineer. Challenge your own work before presenting it.
6. **Autonomous bug fixing** — When given a bug report, just fix it. Point at logs, errors,
   failing tests, then resolve them. Zero context-switching required from the user. Go fix
   failing evals without being told how.

### Task management

1. Write the plan to `tasks/todo.md` with checkable items.
2. Check in before starting implementation.
3. Mark items complete as you go; high-level summary at each step.
4. Add a review section to `tasks/todo.md` when done.
5. Capture lessons in `tasks/lessons.md` after corrections.

### Core principles

- **Simplicity first** — make every change as simple as possible, minimal code impact.
- **No laziness** — find root causes, no temporary fixes, senior-developer standards.
- **Minimal impact** — touch only what's necessary; avoid introducing bugs.
- **Ship fast** — bias toward working software over perfect architecture, but
  NEVER cut the items section 11 marks as "NEVER cut."
- **Determinism over vibes** — choose the more deterministic, more auditable, or cheaper
  option, in that order.

---

## 13. Commands

```bash
# Run / demo
make run COMPETITOR=gusto.com        # Full run for a competitor domain
make demo                            # Curated demo run (golden competitor)
make eval                            # Run the full eval suite (must stay green)

# Python
uv run pytest -v                     # Unit + integration tests
uv run pytest --cov --cov-report=term-missing
uv run ruff check . --fix            # Lint
uv run ruff format .                 # Format (replaces black + isort + flake8)

# Re-run diff (ledger persistence)
make run COMPETITOR=gusto.com        # second invocation diffs against prior ledger
```

`make run` with no competitor starts the conversational loop interactively (Phase 0: prints
hello-loop). Every run ends with the cost/latency line described in section 8.

---

## 14. Coding standards

### Python

- **Python 3.12+** — use `X | None` not `Optional[X]`, `list[T]` not `List[T]`.
- **Type hints required** on all function signatures.
- **Pydantic v2** for all data models (ledger, tool I/O, eval schemas) — use `model_dump()`,
  not `dict()`.
- **Immutable patterns** — frozen dataclasses / Pydantic `frozen=True`; return new objects
  from ledger updates, never mutate claims in place.
- **Async-first** for I/O (HTTP tool calls, Anthropic SDK streaming); sync is fine for pure
  CPU rubric/confidence code.
- **Ruff** for lint + format. Line length 100.
- **Files < 400 lines, functions < 50 lines.** The orchestrator loop stays readable — it's
  the most-read artifact in the repo.
- **Error handling** — every external call: timeout, one retry with backoff, then return a
  typed `ToolFailure`. Never raise out of a tool into the orchestrator. Never silently swallow.

### Prompts

- Prompts live in versioned files under `agent/prompts/`, never inline strings. Bump a
  version comment when behavior changes so `Claim.extracted_by` (model + prompt version)
  stays meaningful for auditability.

### Secrets

- Never hardcode API keys. Load via `python-dotenv` from `.env`; commit only `.env.example`
  with placeholders. Validate required keys are present at startup — fail fast, not mid-run.

---

## 15. Testing strategy

### Coverage target: 80%+

- **Unit tests (must pass before commit):** each tool with a mocked response AND a
  recorded-fixture test; confidence rubric (known inputs → expected score); grounding
  validator mapping; cost math.
- **Integration tests:** orchestrator loop with a stubbed Anthropic client driving a fixed
  tool sequence; ledger persist + re-run diff.
- **Eval layers:** the three layers in section 7 are the real acceptance tests — `make eval`
  must stay green.

### TDD workflow

1. Write test first (RED). 2. Run — fails. 3. Minimal impl (GREEN). 4. Run — passes.
5. Refactor (IMPROVE). 6. Check coverage (80%+).

### Fixtures

- Record fixtures for every external API response so evals run offline and free. Fixtures
  live under `evals/fixtures/` alongside the eval suite.

---

## 16. External APIs & keys (acquire ALL in Phase 0, smoke-test each)

| API | Purpose | Key required | Free tier / budget |
|-----|---------|--------------|--------------------|
| Anthropic | Orchestrator (Sonnet) + extractor (Haiku) + Batch | Yes (`ANTHROPIC_API_KEY`) | Pay per token |
| ScrapeCreators | Meta + Google Ad Library | Yes | 100 free credits |
| Apify | Fallback for Meta ads | Yes | Free tier |
| Firecrawl | Site crawl (`/map`, `/scrape`) | Yes | 500 free credits |
| Exa (or Linkup) | News / press search | Yes | Free tier |
| Wayback CDX | Homepage / pricing history diff | No | Free, throttle gently |
| OpenAI / Google | LLM-as-judge evals (GPT-4o / Gemini) | Yes | Pay per token |
| G2 / Capterra | Public review pages | No | Public pages only |

Total spend target ≤ $30. Log per-run API spend into the cost report (section 8).

---

## 17. Domain glossary (use correct terms in code, briefs, and UI)

| Term | Meaning |
|------|---------|
| ICP | Ideal Customer Profile — the buyer persona a competitor targets |
| EOR | Employer of Record — international hiring without a local entity (Deel's wedge) |
| MDM | Mobile Device Management — Rippling IT Cloud capability with no Gusto equivalent |
| SSO | Single Sign-On — Rippling IT Cloud capability |
| Positioning | How a product is framed relative to alternatives |
| Messaging angle | The specific appeal/hook in copy (price-first, simplicity, global, platform…) |
| Ad longevity | How long an ad has run — long-running ≈ winning creative |
| Claim | A single extracted, evidenced assertion in the ledger |
| Evidence | A source URL + excerpt backing a claim (≥1 enforced per claim) |
| Confidence | Deterministic rubric score (0.0–1.0) computed in Python, never model vibes |
| Grounding | Every brief sentence maps to a claim ID |
| Unverified signal | Claim with confidence < 0.5 — appendix only, never in the brief body |
| Hallucinated URL | A cited URL that 404s and has no Wayback snapshot |
| Wayback diff | Evidence-based "what changed recently" via archived snapshots vs today |

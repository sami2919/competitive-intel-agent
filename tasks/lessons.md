# Lessons — dev-process tracking (not shipped)

Patterns discovered during the build, with the rule that prevents a repeat.

## L1 — ReplayTransport masks missing auth headers

**What happened:** `crawl_site` and `meta_ads` built `HttpRequest` objects with no
auth headers. All 28 tests passed because `ReplayTransport` ignores headers and
`HttpRequest.key()` deliberately excludes headers (so auth-bearing and auth-less
requests hit the same fixture). The gap was invisible until the first live-wiring
attempt, when both tools would have 401'd against Firecrawl / ScrapeCreators.

**Root cause:** The replay seam is correct *by design* (fixtures must be
auth-independent), but that same property makes auth correctness untested by the
suite. "Tests green" did not mean "live works."

**Rule:** When a tool routes through a transport that ignores a dimension (headers,
auth, region), add an explicit live smoke check for that dimension before declaring
the tool live-ready. Each tool must attach its own vendor-specific auth header from
env at call time (`tools/_auth.py`: `bearer` / `x_api_key`), using `.get(..., "")`
so replay/tests stay green. `BaseTool.required_env` declares the env vars so `run()`
validates the union (mode-aware) — adding a tool extends validation automatically.

**Verified schemes (don't re-derive):** Firecrawl = `Authorization: Bearer <key>`;
ScrapeCreators = `x-api-key: <key>` (NOT Bearer — would 401).

## L2 — "Key is set" ≠ "key is valid"; verify value shape, not just presence

**What happened:** `.env` had `ANTHROPIC_API_KEY` present and non-empty, so naive
"are all keys present?" validation passed. But the value had trailing non-comment
junk (`+`, `(`, `)`, `—`) glued after the real key, and even the cleaned 108-char
`sk-ant-api03-…` token was rejected 401 — the key itself was invalid/expired. A
full live run burned a Sonnet intake call before surfacing the 401.

**Root cause:** Presence checks don't catch malformed values or revoked keys. The
`.env.example` line had a trailing comment that the user's paste didn't isolate
cleanly, and the key was stale.

**Rule:** At live-wiring time, run a cheap (~$0.002) model-ID + auth smoke test
against the real Anthropic API *before* any full run: one `messages.create` with
`max_tokens=8` per model ID. Confirms both key validity AND that model IDs resolve
(`claude-sonnet-5`, `claude-haiku-4-5`). Add a clean 401 handler in `run()`
(`_is_auth_error`) so invalid keys exit with a message, not a traceback. When
pasting keys into `.env`, paste ONLY the token — no inline descriptions on the
same line (python-dotenv strips ` # comment` but not `+ … (modes)` junk before a `#`).

## L3 — ANTHROPIC_BASE_URL leaks the real endpoint to a proxy (the 401-that-wasn't)

**What happened:** Two different, well-formed 108-char `sk-ant-api03-` Anthropic keys
both 401'd with "invalid api key" — through the project's `make_client` AND direct SDK
calls. The keys were valid all along: the shell had `ANTHROPIC_BASE_URL=https://api.fireworks.ai/inference`
(set because this Claude Code session runs through Fireworks), and the Anthropic SDK
silently reads that env var. So every "Anthropic" call routed to Fireworks, which
rejects Anthropic keys (it wants Fireworks keys) → 401. The key format was a red
herring; the endpoint was wrong. Forcing `base_url="https://api.anthropic.com"`
authenticated on the first try and all model IDs resolved.

**Root cause:** `anthropic.Anthropic(api_key=…)` with no explicit `base_url` inherits
`ANTHROPIC_BASE_URL` from the environment. In an operator's shell that runs Claude
Code via a proxy (Fireworks/OpenRouter/LiteLLM), that var is set globally and leaks
into `uv run`/`make` subprocesses — silently repointing the project's API calls.

**Rule:** `make_client` MUST pin `base_url="https://api.anthropic.com"` by default
(so the project is immune to whatever proxy the operator's shell uses), with an
opt-in override via a *project-specific* env var (`INTEL_ANTHROPIC_BASE_URL`) — never
the SDK's `ANTHROPIC_BASE_URL` (that's the leaky one). Defense in depth: the Makefile
could also `env -u ANTHROPIC_BASE_URL`, but the code-level pin is the robust fix since
`make_client` is the single seam through which all Anthropic calls flow. Generalizes:
any SDK that auto-reads a base-URL env var (OpenAI, etc.) needs the same explicit pin
in its client factory.

## L4 — Wayback CDX returns OLDEST snapshots first; date-bound the query

**What happened:** `wayback_diff` produced a claim "Gusto is a travel reviews and
lifestyle platform" — gusto.com's *2007 prior-owner* site, not the payroll company.
The CDX query had `limit=100` and no date bounds. CDX returns snapshots in ascending
timestamp order, so `limit=100` returned the 100 OLDEST snapshots (gusto.com's 2007
travel-site era). `_closest_snapshot` then picked 2007 as "closest to 90 days ago"
because no 2026 snapshot was in the truncated result set. Two valid 2026 snapshots
existed but were cut off by the limit.

**Root cause:** CDX default ordering is ascending; an unbounded `limit` truncates to
the oldest N, which for a long-registered domain is a defunct prior-owner era. The
tool trusted CDX to return chronologically representative snapshots.

**Rule:** Always bound a Wayback CDX query with `from`/`to` (YYYYMMDD) set to the
lookback window (from = now - max_lookback - buffer; to = now) so only relevant
recent snapshots are returned. Expose the window computation as a static method
(`_cdx_window`) and have the test mirror it via that same method so the ReplayTransport
fixture key (which includes params) can't drift from the tool's request. Generalizes:
any paginated time-series API with a default-ascending order needs explicit date
bounds, or `limit` silently returns the wrong era.

**Side note (L4a — cache-hit-rate dilution):** only the system prompt + tool schemas
are cached (`cache_control: ephemeral`), not the growing message thread. As tool-result
digests accumulate over a long 8-tool run, the cached prefix shrinks as a fraction of
total input → cache hit rate dropped from 68% (2-tool run) to 9% (8-tool run). The
caching IS engaged (cache_creation tokens flow); the metric just dilutes. Enhancement:
add a cache breakpoint on the last message block to cache the conversation prefix too
(Anthropic supports per-message cache_control). Not a correctness bug — noted for the
cost-report framing.

## L5 — URL health: 403 ≠ 404 (gated pages are LIVE, not hallucinated)

**What happened:** Running `check_urls_sync` on the real Gusto/Deel ledgers classified
real, existing pages as HALLUCINATED — `g2.com/products/deel/reviews`,
`gusto.com/company-news/ai-tax-savings-2026`, `gusto.com/product/pricing` all returned
HTTP 403 (Forbidden) and the url_health module fell through to the Wayback DEAD/
HALLUCINATED path. But 403 means "the URL exists, access denied" (bot-gated, login-
walled, HEAD-blocked) — the opposite of hallucinated. G2 and Gusto's company-news
section block automated HEAD requests with 403.

**Root cause:** the classification only special-cased 2xx/3xx as LIVE; every other
status (403, 404, 5xx) fell through to the "not found → check Wayback" path. 403 and
404 are semantically opposite but were treated the same.

**Rule:** URL-health classification must distinguish status codes by what they mean for
URL *existence*, not just reachability:
  - 2xx/3xx → LIVE
  - 401/403 → LIVE (URL exists, access denied — NOT hallucinated; gated pages are real)
  - 5xx → UNREACHABLE (server error, existence undetermined — transient)
  - 404/410 → DEAD (Wayback snapshot) or HALLUCINATED (no snapshot)
Hallucination detection (arXiv 2604.03173) is about whether a URL *exists*, and 403
is affirmative evidence of existence. After the fix, all 8 sampled real cited URLs
classified LIVE (0 hallucinated) — matching the spec's quality bar. Generalizes: any
"does this resource exist" check must not conflate access-denied with not-found.

## 2026-07-15 — Live regen surfaces what unit tests can't (Phase 6)

**Bug 1 — ad `signal` was dead in production despite green unit tests.** `test_build.py`
passed because hand-crafted raw dicts included `last_seen`. But the live Haiku extractor,
told "never invent dates," omits `last_seen` for active ads (it can't know today's crawl
date and won't guess). So every real ad claim had `last_seen=None` → the signal branch
skipped → 0/26 ad claims classified. The "What's Winning / What Looks Like a Test"
sections had no deterministic signal data behind them.

**Root cause:** asking the LLM for a fact only the crawler knows (today's date).
**Rule:** deterministic facts belong in Python, never in the model. `build.py` now
defaults `last_seen = fetched_at` when `first_seen` is set and `last_seen` is absent —
an observation (the ad is in the active-ads library as of this crawl), not a guess.
Generalizes: at the LLM-output boundary, audit which fields the model is *capable* of
emitting reliably; backstop anything that depends on ground truth only the system has.

**Bug 2 — synthesis returned a blank brief intermittently.** Claude Sonnet-5 emits
thinking blocks by default; `max_tokens` is the *combined* thinking + text budget. For
long-form synthesis the thinking sometimes consumed the entire 4096-token cap
(`stop_reason="max_tokens"`, zero text blocks) → `_text(resp)` returned "" → empty brief.
Flaky because thinking length varies per run.

**Root cause:** model defaults (extended thinking) interact with a token cap sized for
short tool calls, starving long-form output.
**Rule:** for prose-only model calls (synthesis, grounding retries), pass `tools=[]`
(no tool-use escape hatch) AND `thinking={"type":"disabled"}` (don't let thinking eat
the budget). Cheaper, deterministic, no truncation. Generalizes: when a model call must
produce a specific shape (text, not tools), constrain the request to forbid every other
shape — don't rely on the model choosing correctly.

**Meta-lesson:** both bugs were invisible to the stub-based test suite (which models
call sequence, not real model output) and only surfaced via a live run reading the
actual output. A live end-to-end run is a required acceptance gate for any change that
touches the model boundary — green unit tests are not sufficient proof.

## 2026-07-15 — Phase 7 conversational follow-ups: two bugs green stubs hid

**Bug 1 — per-turn step cap vs cumulative counter.** `follow_up` passed `_tool_cycle` a
per-turn cap (`FOLLOWUP_MAX_STEPS=4`), but `_tool_cycle`'s guard is
`session.steps < max_steps` and `session.steps` is cumulative across the whole session.
After an 8-tool initial run, `8 < 4` is always False — every follow-up tool call was
silently dropped in production. The stubbed suite's initial run used only 1 step, so
`1 < 4` passed and hid it; the live gate used `tools=[]` (no tool calls) so it never
exercised the path. A code review reading the guard caught it.

**Root cause:** a per-phase cap compared against a cumulative counter can never agree
after the first phase.
**Rule:** when a loop guard uses a cumulative counter (`session.steps`), a per-turn cap
of N must be passed as `counter_at_entry + N`, not N (or track a per-turn counter reset
each entry). Generalizes: any "budget per phase" compared against a "total so far"
counter is wrong by construction once phase 1 consumes any of the total.

**Bug 2 — run-again prefix list missed natural speech.** The router matched "run again
for X" but not the user's literal "run this again for X" (doesn't start with "run again").
The test used "run again for gusto" (no "this"), so it passed while the real phrasing
broke. An adversarial prober reasoning about language variants caught it.

**Root cause:** a prefix table only matches the exact phrases you listed; natural speech
has variants ("run it again", "do it again", "start over") you didn't enumerate.
**Rule:** for a deterministic NL router, prefer a regex that captures the *shape*
(`^(run|do)\s+(this|it|that)?\s*again|start\s+(over|fresh)|...`) over a prefix list, and
seed the tests with the user's *literal* example phrasings — not just the canonical form
you implemented. Adversarial verification (a second reader probing variants the author
didn't think of) is what catches this; green pattern-tests don't.

**Meta-lesson (reprise):** two reviewers — one reading code, one probing language —
found 5 real issues the green stubbed suite missed. Green tests prove the code does what
the tests check, not what the user will type. Adversarial verification + a live gate on
the real model boundary are required gates, not optional.

## L6 — README commands must be executed verbatim in a clean shell before shipping

**What happened:** The README quickstart step 1 said `uv sync --with-requirements
requirements.txt`. That flag exists on `uv run` but NOT on `uv sync`, so the very first
command a reviewer types fails with `error: unexpected argument '--with-requirements'
found`. It was never caught because the author path always went through `make` targets
(which correctly use `uv run --with-requirements`), and `pyproject.toml` declares no
dependencies anyway — so `uv sync` would have installed nothing even if the flag existed.

**Root cause:** the quickstart was written by analogy to the working Makefile invocation
instead of being executed. Docs drift is invisible to the test suite: no eval runs the
README.

**Rule:** before shipping, copy-paste every README code block verbatim into a clean
shell (fresh clone, no venv) and require it to succeed. A quickstart command that has
never been executed is a hallucinated command — same failure class as a hallucinated
URL, and it hits the reviewer at minute zero, when it costs the most.

**Fix applied:** step 1 now documents that `make` targets self-resolve deps via
`uv run --with-requirements requirements.txt`, with an optional verified pre-warm
command. Also corrected the Makefile `demo:` comment and README §Tools line that
falsely claimed `make demo` runs offline via ReplayTransport (it uses the live
transport — DECISIONS §15 / failure_log F5).

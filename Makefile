COMPETITOR ?=
PAGE ?=
SESSIONS ?= 100000
UV := uv run --with-requirements requirements.txt
.PHONY: run demo eval eval-live eval-judge eval-urls cro test lint format

# Live run. With COMPETITOR set, analyzes that competitor.
# Without it, starts the conversational loop interactively.
run:
	INTEL_MODE=live $(UV) python -m agent.repl $(COMPETITOR)

# Demo run on the golden competitor. INTEL_MODE=demo relaxes data-API key validation,
# but the CLI still uses the LIVE transport (offline ReplayTransport wiring is
# DECISIONS §15 / failure_log F5). Fully offline + free = `make eval`.
demo:
	INTEL_MODE=demo $(UV) python -m agent.repl gusto.com

# CRO agent: generate landing-page variants gated by the competitor ledger.
# Reuses the intel agent's tool layer, claim ledger, and cost accounting.
# Requires a shipped ledger: outputs/$(COMPETITOR)_canonical.json.
#   make cro PAGE=https://www.rippling.com/payroll COMPETITOR=gusto.com
cro:
	INTEL_MODE=live $(UV) python -m cro.run --page $(PAGE) --competitor $(COMPETITOR) --sessions $(SESSIONS)

# Full eval suite — must stay green (offline, free — stubbed Anthropic client).
eval test:
	$(UV) pytest -v

# Live acceptance gate (Phase 7): a REAL follow-up on the persisted Gusto ledger.
# Opt-in — calls the real Anthropic API (~$0.01). NOT part of `make eval`; run before
# shipping any change to the model boundary (lessons.md: stubs can't catch regen bugs).
eval-live:
	INTEL_LIVE=1 $(UV) pytest -v -s evals/test_live_followup.py

# Layer-2 cross-family LLM judge (CLAUDE.md §7): a different model family (GPT-4o-mini
# or Gemini 2.5 Flash) judges the produced ledger + brief. Needs OPENAI_API_KEY or
# GEMINI_API_KEY + a pre-built ledger (`make run COMPETITOR=<slug>`). Skips cleanly
# without a key. NOT part of `make eval` — costs a few cents.
eval-judge:
	INTEL_MODE=live $(UV) python -m evals.judge $(COMPETITOR)

# URL-health sweep over ALL shipped ledgers (outputs/*_intel.json): HEAD-check every
# cited URL, Wayback-CDX fallback, exit 1 on any HALLUCINATED URL (arXiv 2604.03173).
# Network-bound (free, no keys) — separate from the offline `make eval`.
eval-urls:
	$(UV) python -m evals.run_url_health

lint:
	$(UV) ruff check . --fix

format:
	$(UV) ruff format .

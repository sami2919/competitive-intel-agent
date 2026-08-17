# Interactive REPL + name/domain input — design

Date: 2026-07-14 · Status: approved (approach A: live-thread session)

## Problem

The assignment requires "takes a competitor's **name or domain** as input" and a
conversational agent ("Be conversational" is a graded axis). Today:

- `make run` (no `COMPETITOR`) hits a Phase-3 stub — no interactive mode exists.
- Every tool takes a `domain` and builds `https://{domain}`; typing "BambooHR"
  produces `https://BambooHR` and fails. No name→domain resolution.
- The clarifying question is auto-answered `"both"` (loop.py) — the agent never
  actually pauses and asks the user.
- Follow-ups ("dig deeper on pricing", "run this again for Deel") aren't wired.

## Design

### 1. Entry points & modes

- `make run` (no `COMPETITOR`) → interactive REPL (replaces the stub).
- `make run COMPETITOR=gusto.com` → unchanged one-shot mode; clarifying Q
  auto-answered `"both"` (scripted runs stay reproducible).
- `make demo` → unchanged.
- Key validation (`missing_keys`) runs before the REPL starts, same as one-shot.

### 2. Name→domain resolver — `agent/resolve.py` (deterministic)

`resolve_competitor(text, transport, client=None) -> Resolution(domain, method, note)`

- Input contains a dot & no spaces → it's a domain: strip scheme/path/whitespace,
  lowercase → `method="domain-input"` (no network call).
- Else: slug = lowercase, strip non-alphanumerics → HEAD `https://{slug}.com`
  via the shared transport (5s timeout). 2xx/3xx → `method="heuristic-head"`.
- HEAD fails and a client is available → one Sonnet call (prompt:
  `agent/prompts/resolver_v1.md`) suggesting the likely domain; HEAD-verify it
  → `method="llm-suggested"`. Otherwise `method="unresolved"` (domain=None).
- The REPL always confirms before spending credits:
  `Resolved 'Bamboo HR' → bamboohr.com — proceed? [Y / n / type the domain]`.
  Typing a domain at the confirm prompt overrides. Unresolved → ask the user
  to type the domain.

### 3. Session — `agent/session.py` (approach A: live thread)

`Session` dataclass owning `competitor, messages, ledger, claim_counter, cost,
canonical_claims, steps`. `run_competitor` builds a Session at the top and
operates on it throughout; `RunResult` gains a `session` field so the REPL can
keep the thread alive.

- The clarifying question becomes an `ask_user: Callable[[str], str] | None`
  callback: interactive mode reads real input; one-shot passes the static
  `clarifying_answer="both"` as today.
- `follow_up(session, text, client, tools, ...) -> str` appends the user turn to
  the **existing** message thread and re-enters the same tool cycle — the
  orchestrator already holds the claim digests in cached context, so it decides
  whether to answer from the ledger or call more tools (the DRY(E) demo).
- Follow-up answers are grounded with the same validator + retry loop as the
  brief (shared helper extracted from loop.py).
- Outputs: a follow-up re-persists `{slug}_intel.json` (the ledger may have
  grown) and prints the answer in-chat. The brief file is only rewritten by a
  full `analyze` — follow-up answers are conversation, not the brief.

### 4. REPL — `agent/repl.py` (deterministic router, no LLM)

- `analyze <name-or-domain>` — or any input when no session is active — resolves,
  confirms, starts a fresh session, runs, writes outputs (+ re-run diff vs prior
  ledger, as today).
- Any other text with an active session → follow-up on the live thread.
- `analyze <other>` mid-session → fresh session ("run this again for Deel").
- `quit` / `exit` / Ctrl-C / Ctrl-D → clean exit printing the cumulative
  session cost line.
- Each turn is wrapped: an exception reports and returns to the prompt; auth
  errors keep the existing fatal message.

### 5. Budget & errors

Step budget (35) is per-session — follow-ups share the cap so a chatty session
can't run away. Resolver failures are prompts, never crashes. All HTTP goes
through the shared transport (timeout + one retry + typed failure).

### 6. Testing

- `evals/test_resolve.py`: domain passthrough, URL stripping, spaced names →
  slug.com (fake transport), HEAD-fail → LLM fallback (stub client), unresolved.
- `evals/test_session.py`: follow-up appends to the same thread (no reset),
  ledger grows across turns, ask_user called exactly once, new-competitor resets.
- Existing 129 tests stay green; `make eval` is the gate.

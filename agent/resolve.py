"""Name→domain resolution — deterministic first, LLM fallback, always user-confirmed.

The assignment takes "a competitor's name or domain" as input, but every tool takes a
domain. Resolution ladder (spec: docs/superpowers/specs/2026-07-14-interactive-repl-design.md):

  1. input has a dot          -> it IS a domain: normalize, no network call
  2. slugified name + ".com"  -> HEAD-verify via the shared transport (free, deterministic)
  3. one Sonnet call          -> suggest the likely domain, then HEAD-verify THAT
  4. otherwise                -> unresolved: the REPL asks the user to type the domain

Nothing downstream trusts the resolver blind — the REPL always echoes
"Resolved 'X' → x.com — proceed?" before any API credits are spent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent.cost import Usage
from agent.llm import load_prompt
from tools._transport import HttpRequest, Transport, TransportError

RESOLVER_MODEL = "claude-sonnet-5"  # same pin/fallback policy as the orchestrator (D4)
RESOLVER_PROMPT_VERSION = "resolver_v1"

_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$")


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving user input to a domain.

    method: "domain-input" | "heuristic-head" | "llm-suggested" | "unresolved"
    usage:  tokens spent (non-zero only on the LLM-fallback path) — flows into
            the session cost accumulator.
    """

    domain: str | None
    method: str
    note: str = ""
    usage: Usage = Usage()


def looks_like_domain(text: str) -> bool:
    """True when the input is already a domain/URL rather than a company name."""
    t = text.strip()
    return "." in t and " " not in t


def normalize_domain(text: str) -> str:
    """Lowercase and strip scheme/path/query: 'https://Gusto.com/pricing?x' -> 'gusto.com'."""
    d = text.strip().lower()
    d = d.split("://")[-1]
    d = d.split("/")[0]
    return d.split("?")[0]


async def _head_ok(domain: str, transport: Transport) -> bool:
    """HEAD https://{domain} through the shared transport; 2xx/3xx counts as alive."""
    try:
        resp = await transport.request(HttpRequest(method="HEAD", url=f"https://{domain}"))
    except TransportError:
        return False
    return 200 <= resp.status < 400


def _ask_llm(name: str, client: Any) -> tuple[str | None, Usage]:
    """One cheap Sonnet call: company name -> candidate domain (or None)."""
    prompt = load_prompt(RESOLVER_PROMPT_VERSION)
    resp = client.messages.create(
        model=RESOLVER_MODEL,
        max_tokens=64,
        system=prompt,
        messages=[{"role": "user", "content": name}],
    )
    usage = Usage.from_sdk(resp.usage)
    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text = block.text.strip().strip("`").lower()
            break
    if not text or text == "unknown":
        return None, usage
    candidate = normalize_domain(text)
    return (candidate, usage) if _DOMAIN_RE.match(candidate) else (None, usage)


async def resolve_competitor(
    text: str, transport: Transport, client: Any | None = None
) -> Resolution:
    """Resolve user input (name or domain) to a domain via the deterministic ladder."""
    if looks_like_domain(text):
        return Resolution(domain=normalize_domain(text), method="domain-input")

    slug = re.sub(r"[^a-z0-9]", "", text.lower())
    if slug:
        candidate = f"{slug}.com"
        if await _head_ok(candidate, transport):
            return Resolution(
                domain=candidate,
                method="heuristic-head",
                note=f"HEAD https://{candidate} responded",
            )

    if client is not None:
        suggestion, usage = _ask_llm(text, client)
        if suggestion and await _head_ok(suggestion, transport):
            return Resolution(
                domain=suggestion,
                method="llm-suggested",
                note=f"Sonnet suggested {suggestion}; HEAD-verified",
                usage=usage,
            )
        return Resolution(
            domain=None,
            method="unresolved",
            note="heuristic + LLM suggestion both failed HEAD verification",
            usage=usage,
        )

    return Resolution(
        domain=None, method="unresolved", note=f"HEAD https://{slug}.com failed; no LLM fallback"
    )

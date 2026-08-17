"""Name→domain resolver tests — deterministic paths + LLM fallback (all offline).

Covers the spec's resolution ladder:
  domain input -> passthrough (no network)
  company name -> slug.com verified by HEAD
  HEAD fails   -> Sonnet suggestion, HEAD-verified
  no dice      -> unresolved (REPL asks the user)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from agent.resolve import Resolution, looks_like_domain, normalize_domain, resolve_competitor
from evals.stub import StubClient, StubResponse, TextBlock
from tools._transport import HttpRequest, HttpResponse, TransportError


@dataclass
class FakeTransport:
    """Scripted HEAD responses keyed by URL; records every request it sees."""

    responses: dict[str, HttpResponse | Exception] = field(default_factory=dict)
    seen: list[HttpRequest] = field(default_factory=list)

    async def request(self, req: HttpRequest) -> HttpResponse:
        self.seen.append(req)
        r = self.responses.get(req.url)
        if r is None:
            raise TransportError(f"unscripted: {req.url}")
        if isinstance(r, Exception):
            raise r
        return r


# --- pure helpers -----------------------------------------------------------


def test_looks_like_domain():
    assert looks_like_domain("gusto.com")
    assert looks_like_domain("https://deel.com/pricing")
    assert not looks_like_domain("Bamboo HR")
    assert not looks_like_domain("Justworks")


def test_normalize_domain_strips_scheme_path_query():
    assert normalize_domain("https://Gusto.com/pricing?x=1") == "gusto.com"
    assert normalize_domain("deel.com") == "deel.com"
    assert normalize_domain("  ADP.com  ") == "adp.com"


# --- resolution ladder ------------------------------------------------------


def test_domain_input_passthrough_no_network():
    t = FakeTransport()
    res = asyncio.run(resolve_competitor("https://gusto.com/pricing", t))
    assert res == Resolution(domain="gusto.com", method="domain-input", note=res.note)
    assert t.seen == []  # no HEAD for explicit domains


def test_name_resolves_via_slug_head():
    t = FakeTransport(responses={"https://bamboohr.com": HttpResponse(status=301, body="")})
    res = asyncio.run(resolve_competitor("Bamboo HR", t))
    assert res.domain == "bamboohr.com"
    assert res.method == "heuristic-head"
    assert t.seen[0].method == "HEAD"


def test_head_failure_falls_back_to_llm_suggestion():
    t = FakeTransport(
        responses={
            "https://acmepayrollco.com": HttpResponse(status=404, body=""),
            "https://acme-payroll.io": HttpResponse(status=200, body=""),
        }
    )
    client = StubClient(
        sonnet_script=[StubResponse(content=[TextBlock("acme-payroll.io")])], haiku_text="[]"
    )
    res = asyncio.run(resolve_competitor("Acme Payroll Co", t, client=client))
    assert res.domain == "acme-payroll.io"
    assert res.method == "llm-suggested"


def test_unresolvable_returns_unresolved():
    t = FakeTransport()  # every HEAD raises
    client = StubClient(
        sonnet_script=[StubResponse(content=[TextBlock("nope.example")])], haiku_text="[]"
    )
    res = asyncio.run(resolve_competitor("Totally Unknown Startup", t, client=client))
    assert res.domain is None
    assert res.method == "unresolved"


def test_no_client_head_failure_is_unresolved():
    t = FakeTransport()  # HEAD raises, no client to fall back to
    res = asyncio.run(resolve_competitor("Mystery Co", t))
    assert res.domain is None
    assert res.method == "unresolved"

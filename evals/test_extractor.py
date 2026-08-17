"""Extractor tests — batched Haiku extraction with per-claim source_url (D7).

Uses the stub client so no API key is needed. Verifies JSON parsing from a model
response that may have surrounding prose, and that claims without source_url are dropped.
"""

from __future__ import annotations

from agent.extractor import _parse_claims, extract_claims_sync
from agent.llm import load_prompt
from evals.stub import StubClient

HAIKU_JSON = """
Here are the claims:
[
  {"statement": "Gusto leads with simple flat-rate pricing", "category": "pricing",
   "source_url": "https://gusto.com/pricing", "excerpt": "Pricing from $39/mo + $6/user."},
  {"statement": "Gusto targets small businesses", "category": "icp_targeting",
   "source_url": "https://gusto.com", "excerpt": "Payroll for small business."}
]
Done.
"""


def test_parse_claims_extracts_json_array_from_prose():
    claims = _parse_claims(HAIKU_JSON)
    assert len(claims) == 2
    assert claims[0]["category"] == "pricing"
    assert claims[0]["source_url"] == "https://gusto.com/pricing"


def test_parse_claims_drops_missing_source_url():
    text = '[{"statement": "no source", "category": "messaging"}]'
    claims = _parse_claims(text)
    assert claims == []


def test_parse_claims_empty_on_no_json():
    assert _parse_claims("no json here") == []


def test_extract_claims_sync_via_stub():
    client = StubClient(sonnet_script=[], haiku_text=HAIKU_JSON)
    prompt = load_prompt("extractor_v1")
    claims, usage = extract_claims_sync("### SOURCE: https://gusto.com\n...", client, prompt)
    assert len(claims) == 2
    assert usage.input_tokens == 100  # stub usage

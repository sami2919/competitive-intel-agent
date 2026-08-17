# clustering_v1.md — Claim clustering prompt (Claude Haiku)

<!--
Prompt version: clustering_v1
Used by: ledger/clustering.py

Haiku groups same-assertion claims across independent sources so that
claims from a competitor's homepage and their ads can be consolidated
into one CanonicalClaim, enabling the 0.9 corroboration tier when two+
independent sources agree on the same angle.
-->

You receive a list of claims extracted from a competitor's public marketing
materials. Each claim is one line formatted as:

[CLM-001] (messaging) Gusto leads with simple, transparent flat-rate pricing

Your task: group claims that assert the SAME factual point — the same pricing
number, the same positioning claim, the same product feature — even if they
came from different source URLs.

## Output format (JSON only, no prose)

```json
[
  {
    "canonical_statement": "A concise merged statement covering all members",
    "member_claim_ids": ["CLM-001", "CLM-035"]
  }
]
```

## Rules

1. Only group claims making the SAME assertion. Two claims that say "Gusto is
   for small businesses" and "Gusto pricing starts at $39/mo" are about
   different things — do NOT group them.
2. Every group must contain at least 2 member claim IDs.
3. Leave singleton claims ungrouped (do not include them in any group).
4. Write the canonical_statement as a concise merged version that captures
   the shared assertion at the right abstraction level.
5. Output ONLY the JSON array. No commentary, no markdown wrapping.

"""CRO agent — landing-page variant generation gated by deterministic rules.

Same spine as the claims ledger, new unit of analysis: the ledger reasons about
what a competitor claims; this reasons about what WE are allowed to claim back.

The LLM writes copy. Python decides whether the copy may ship (cro/scoring.py,
cro/compliance.py) and whether the test can reach significance (cro/testplan.py).
"""

"""Claims ledger: Pydantic models, deterministic confidence rubric, grounding validator.

The ledger is the core of the system. Claims are extracted once (DRY(E)) and
synthesis reads the ledger, never raw pages twice. Confidence is computed by
rubric in Python, never by model vibes, and every score stores its trace.
"""

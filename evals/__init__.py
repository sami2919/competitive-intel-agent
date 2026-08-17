"""Eval suite — three layers + golden set + failure log.

Layer 1: deterministic pytest checks (schema, evidence, grounding, url health, confidence recompute).
Layer 2: cross-family LLM judge (GPT/Gemini via Batch API).
Layer 3: trajectory asserts (step budget, skip-empty, clarifying Q, no loops).
"""

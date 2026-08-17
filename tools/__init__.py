"""Deterministic source tools. One module per source, Pydantic I/O, typed failures.

All tools subclass BaseTool and route external calls through the shared transport
(LiveTransport / ReplayTransport) — the single seam that makes offline demo mode
and recorded-fixture evals possible (eng review D1/D3).
"""

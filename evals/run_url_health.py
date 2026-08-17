"""URL-health sweep over the SHIPPED ledgers — `make eval-urls`.

The classifier (evals/url_health.py) was unit-tested but only ever run manually
against real outputs, so the README's "0 hallucinated URLs" was a hand-run claim.
This script makes it a measured, repeatable one: it collects every distinct cited
URL from outputs/*_intel.json, HEAD-checks each (with Wayback CDX fallback), prints
a per-ledger table + totals, and exits non-zero if any URL classifies HALLUCINATED
(the arXiv 2604.03173 failure mode we measure ourselves against).

Network-bound by design — run it as its own gate, not inside the offline `make eval`.

Usage:
    uv run --with-requirements requirements.txt python -m evals.run_url_health
    make eval-urls
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from evals.url_health import check_urls_sync


def _cited_urls(intel_path: Path) -> list[str]:
    claims = json.loads(intel_path.read_text(encoding="utf-8"))
    urls = {e["source_url"] for c in claims for e in c.get("evidence", [])}
    return sorted(urls)


def main() -> int:
    intel_files = sorted(Path("outputs").glob("*_intel.json"))
    if not intel_files:
        print("no outputs/*_intel.json ledgers found — run `make run COMPETITOR=...` first")
        return 1

    grand_totals: Counter[str] = Counter()
    hallucinated: list[str] = []
    for intel in intel_files:
        urls = _cited_urls(intel)
        results = check_urls_sync(urls)
        counts = Counter(r.status for r in results)
        grand_totals.update(counts)
        print(f"\n{intel.name}: {len(urls)} distinct cited URLs")
        for status in ("LIVE", "DEAD", "HALLUCINATED", "UNREACHABLE"):
            if counts[status]:
                print(f"  {status:12} {counts[status]}")
        for r in results:
            if r.status == "HALLUCINATED":
                hallucinated.append(r.url)
                print(f"  !! HALLUCINATED: {r.url} ({r.detail})")
            elif r.status in ("DEAD", "UNREACHABLE"):
                print(f"  -- {r.status}: {r.url} ({r.detail})")

    total = sum(grand_totals.values())
    pct = (len(hallucinated) / total * 100) if total else 0.0
    print(
        f"\nTOTAL: {total} URLs · LIVE {grand_totals['LIVE']} · DEAD {grand_totals['DEAD']} · "
        f"HALLUCINATED {grand_totals['HALLUCINATED']} ({pct:.1f}%) · "
        f"UNREACHABLE {grand_totals['UNREACHABLE']}"
    )
    print("(benchmark: deep-research agents hallucinate 3-13% of cited URLs — arXiv 2604.03173)")
    return 1 if hallucinated else 0


if __name__ == "__main__":
    sys.exit(main())

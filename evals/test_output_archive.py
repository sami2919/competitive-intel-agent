"""Archive-before-overwrite (failure_log F15): a re-run during a bad-luck window
(Wayback 503, Meta empty) must never destroy the prior good outputs — the previous
brief/ledger/canonical files move to outputs/history/{slug}/{timestamp}/ first.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.loop import RunResult
from agent.repl import _write_outputs


def _result(competitor: str = "gusto.com") -> RunResult:
    return RunResult(competitor=competitor, brief="# New brief\n", ledger=[])


def test_prior_outputs_archived_before_overwrite(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = Path("outputs")
    out.mkdir()
    (out / "gusto.com_brief.md").write_text("# Old brief\n", encoding="utf-8")
    (out / "gusto.com_intel.json").write_text("[]", encoding="utf-8")

    _write_outputs(_result())

    # New files written
    assert (out / "gusto.com_brief.md").read_text(encoding="utf-8") == "# New brief\n"
    # Old files archived under outputs/history/gusto.com/<timestamp>/
    history = list((out / "history" / "gusto.com").glob("*/gusto.com_brief.md"))
    assert len(history) == 1
    assert history[0].read_text(encoding="utf-8") == "# Old brief\n"
    archived_ledger = history[0].parent / "gusto.com_intel.json"
    assert json.loads(archived_ledger.read_text(encoding="utf-8")) == []


def test_first_run_no_archive_dir_created(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    _write_outputs(_result())

    assert (Path("outputs") / "gusto.com_brief.md").exists()
    assert not (Path("outputs") / "history").exists()

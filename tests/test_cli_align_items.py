"""The ``align-items`` command after the C1 / M2 review: replay versus buying in the help and the printed report, calls actually
made (and failed), the provenance sidecar among the written files, a dead endpoint exiting 4. ``run_align_items`` is faked."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from semigraph import cli
from semigraph.cli import app
from semigraph.config import Settings
from semigraph.graph import adjudicate as adj
from semigraph.graph import items

runner = CliRunner()
SUMMARY = [{"pair_id": "NVDA-a-b", "ticker": "NVDA", "older_date": "d", "newer_date": "e", "compared": True,
            "not_compared_reason": None, "older_items": 2, "newer_items": 2, "older_unchanged": 1, "older_reworded": 1,
            "older_merged": 0, "older_removed": 0, "older_uncertain": 0, "newer_carried": 2, "newer_new": 0, "newer_uncertain": 0,
            "passages_removed": 1, "passages_reworded": 0, "passages_added": 0, "adjudicated": 0, "band": 7, "band_answered": 3}]


@pytest.fixture
def fake_run(monkeypatch, tmp_path):
    state = {"calls": [], "raises": None, "run": {}}

    def fake(settings, tickers=None, **kwargs):
        state["calls"].append({"tickers": tickers, **kwargs})
        if state["raises"]:
            raise state["raises"]
        return items.AlignRun(SUMMARY, None, state["run"].pop("written", []), bool(kwargs.get("dry_run")), **state["run"])

    monkeypatch.setattr(cli, "_settings", lambda: Settings(data_dir=tmp_path / "data", _env_file=None))
    monkeypatch.setattr(items, "run_align_items", fake)
    return state


def help_text():
    """The docstring and every option's help of the command, whitespace-collapsed (independent of the terminal's wrapping)."""
    import typer.main

    command = typer.main.get_command(app).commands["align-items"]
    return " ".join(" ".join([command.help or "", *[str(p.help or "") for p in command.params]]).split())


def test_the_help_says_every_run_replays_recorded_answers_and_only_the_flags_buy():
    text = help_text()
    assert "Every run REPLAYS, for free, every recorded model answer" in text and "Only --adjudicate / --adjudicate-passages" in text
    assert "legacy pas-v2 answers" not in text and "Pass it again to replay" not in text        # the two statements C1 made false
    assert "a run without either flag buys nothing" in text and "Recorded answers are replayed by every run" in text


def test_a_plain_run_reports_the_recorded_item_and_passage_verdicts_it_replayed_and_that_no_model_was_called(fake_run):
    fake_run["run"] = {"item_verdicts_used": 26, "passage_verdicts_used": 12, "passage_prompt_version": "pas-v3", "below_replayed": True}
    out = runner.invoke(app, ["align-items"]).output
    assert "26 recorded item verdict(s) applied from adjudications.jsonl" in out and "no model was called" in out
    assert "12 cached passage verdict(s) applied" in out and "pas-v3 answers" in out and "below-band answers included" in out


def test_the_calls_reported_are_the_calls_made_with_the_failed_ones_named(fake_run):
    fake_run["run"] = {"item_calls": 5, "item_failed": 1, "item_verdicts_used": 4, "passage_calls": 9, "passage_failed": 2,
                       "passage_verdicts_used": 7}
    out = runner.invoke(app, ["align-items", "--adjudicate", "--adjudicate-passages"]).output
    assert "Item adjudication: 5 call(s) made (1 failed" in out and "4 verdict(s) applied" in out
    assert "Passage adjudication: 9 call(s) made (2 failed" in out and "7 verdict(s) applied" in out
    assert "asked again by the next run" in out


def test_no_calls_line_is_printed_when_nothing_was_bought(fake_run):
    out = runner.invoke(app, ["align-items"]).output
    assert "call(s) made" not in out


def test_the_written_files_include_the_provenance_sidecar(fake_run, tmp_path):
    sidecar = tmp_path / "risk_alignment" / "alignment_provenance.json"
    fake_run["run"] = {"written": [tmp_path / "NVDA_pairs.parquet", sidecar]}
    out = runner.invoke(app, ["align-items"]).output
    assert str(sidecar) in out


def test_a_plain_run_passes_no_flag_and_no_embedder_and_the_default_budget_buys_nothing(fake_run):
    runner.invoke(app, ["align-items"])
    call = fake_run["calls"][0]
    assert call["adjudicate"] is False and call["adjudicate_passages"] is False and "embed" not in call


def test_a_dead_endpoint_exits_4_and_says_what_was_kept(fake_run):
    fake_run["raises"] = adj.CallsFailing("8 consecutive calls failed (last: RuntimeError: 401): nothing further was spent; "
                                          "the answers bought so far are checkpointed")
    result = runner.invoke(app, ["align-items", "--adjudicate-passages"])
    assert result.exit_code == 4 and "consecutive calls failed" in result.output and "checkpointed" in result.output


def test_the_run_function_and_the_command_stay_under_the_function_length_rule():
    import ast
    import inspect

    for func in (items.run_align_items, items._write_tables, items._purchases, cli.align_items_cmd):
        node = ast.parse(inspect.getsource(func)).body[0]
        assert node.end_lineno - node.lineno + 1 <= 50, f"{func.__name__} is {node.end_lineno - node.lineno + 1} lines"


def test_the_source_of_the_pipeline_files_stays_under_the_soft_ceiling():
    root = Path(items.__file__).parent
    for name in ("items.py", "adjudicate.py", "passage_adjudicate.py", "alignment.py", "alignment_provenance.py"):
        assert len((root / name).read_text(encoding="utf-8").splitlines()) < 800, name

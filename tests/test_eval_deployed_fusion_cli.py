"""M2: eval-deployed measures the fused retrieval when RETRIEVAL_FUSION is on, and never spends on an unfused run by accident."""

import json

import pytest
from typer.testing import CliRunner

from semigraph import cli
from semigraph.config import Settings

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts").mkdir()
    return tmp_path


def _wire(env, monkeypatch, *, flag, runtime):
    from semigraph.eval import bakeoff as bo
    from semigraph.graph import client
    from semigraph.retrieval import fusion
    from semigraph.serve import store
    from semigraph import embeddings

    calls = {"run": [], "built": []}

    class FakeDriver:
        def close(self):
            pass

    monkeypatch.setattr(cli, "_settings", lambda: Settings(
        _env_file=None, data_dir=env / "data", answer_model="openai/gpt-6-luna", escalation_model="anthropic/claude-sonnet-5",
        retrieval_fusion=flag))
    monkeypatch.setattr(client, "get_driver", lambda settings=None: FakeDriver())
    monkeypatch.setattr(embeddings, "Embedder", lambda: object())
    monkeypatch.setattr(store, "current_snapshot", lambda d: {"id": "S1"})
    monkeypatch.setattr(fusion, "process_runtime", lambda d, s, sid: calls["built"].append(sid) or runtime)
    monkeypatch.setattr(bo, "check_deployed_runs_resumable", lambda p: None)
    monkeypatch.setattr(bo, "run_deployed", lambda *a, **kw: calls["run"].append(kw) or [])
    keys = ("mechanical", "citation_validity", "routes", "escalated", "escalated_ids", "checks_failed", "rows_without_checks",
            "avg_cost_usd", "total_cost_usd", "avg_latency_s", "errors")
    scored = {**{k: 0 for k in keys}, "judged": {"open_correct": 0, "open_of": 0, "votes": 3}}
    monkeypatch.setattr(bo, "score_deployed", lambda rows, bench, judge, **kw: scored)
    return calls


@pytest.mark.parametrize("flag", [True, False])
def test_eval_deployed_passes_the_fusion_runtime_only_with_the_flag_on(env, monkeypatch, flag):
    runtime = object()
    calls = _wire(env, monkeypatch, flag=flag, runtime=runtime)
    result = runner.invoke(cli.app, ["eval-deployed", "--max-usd", "1", "--yes", "--report-name", "r.json"])
    assert result.exit_code == 0, result.output
    assert calls["run"][0]["fusion_runtime"] is (runtime if flag else None)
    assert calls["built"] == (["S1"] if flag else [])
    assert json.loads((env / "artifacts" / "r.json").read_text(encoding="utf-8"))["fusion"] is flag


def test_eval_deployed_with_the_flag_on_stops_before_spending_when_the_index_did_not_build(env, monkeypatch):
    calls = _wire(env, monkeypatch, flag=True, runtime=None)
    result = runner.invoke(cli.app, ["eval-deployed", "--max-usd", "1", "--yes", "--report-name", "r.json"])
    assert result.exit_code != 0 and "fusion" in result.output.lower()
    assert calls["run"] == []


def test_eval_deployed_writes_a_stripped_report_and_the_costs_to_a_sidecar(env, monkeypatch):
    from semigraph.eval import bakeoff as bo

    calls = _wire(env, monkeypatch, flag=False, runtime=None)
    scored = bo.score_deployed(None, None, None)
    scored = {**scored, "avg_cost_usd": 0.014, "total_cost_usd": 0.84}
    monkeypatch.setattr(bo, "score_deployed", lambda rows, bench, judge, **kw: scored)
    result = runner.invoke(cli.app, ["eval-deployed", "--max-usd", "1", "--yes", "--report-name", "r.json"])
    assert result.exit_code == 0, result.output
    assert "0.84" in result.output                                               # the console still prints the cost
    text = (env / "artifacts" / "r.json").read_text(encoding="utf-8")
    assert "cost_usd" not in text and "0.84" not in text
    side = json.loads((env / "data" / "processed" / "costs" / "r.costs.json").read_text(encoding="utf-8"))
    assert side["removed"]["/total_cost_usd"] == 0.84 and side["removed"]["/avg_cost_usd"] == 0.014
    assert calls["run"]

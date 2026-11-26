"""M2 R24: the owner-run probe that times the MiniLM rerank stage on the Fly machine (scripts/rerank_latency_probe.py).

The gate is that rerank adds at most 2 s at p95 on the throttled machine. No log line carries the stage's time, and Turnstile blocks
scripted asks, so the probe loads the same reranker the service loads and times ``rerank`` on worst-case inputs (the deepest
candidate list, passages long enough to fill the 384-token window), reading the cgroup's CPU throttling before and after.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rerank_latency_probe", ROOT / "scripts" / "rerank_latency_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class FakeReranker:
    def __init__(self, clock, seconds):
        self.clock, self.seconds, self.calls = clock, list(seconds), []

    def rerank(self, query, candidates):
        self.calls.append((query, list(candidates)))
        self.clock.now += self.seconds.pop(0)
        return [0.0] * len(candidates)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_percentile_is_nearest_rank():
    values = [float(v) for v in range(1, 21)]          # 1..20
    assert probe.percentile(values, 95) == 19.0 and probe.percentile(values, 50) == 10.0
    assert probe.percentile([3.0], 95) == 3.0
    with pytest.raises(ValueError):
        probe.percentile([], 95)


def test_time_rerank_times_each_run_and_passes_the_whole_candidate_list():
    clock = Clock()
    fake = FakeReranker(clock, [0.5, 1.0, 1.5, 2.5])
    result = probe.time_rerank(fake, "q", ["a", "b", "c"], runs=4, warmup=0, clock=clock)
    assert result == {"candidates": 3, "runs": 4, "p50_s": 1.0, "p95_s": 2.5, "max_s": 2.5}
    assert all(c == ("q", ["a", "b", "c"]) for c in fake.calls)


def test_warmup_runs_are_not_counted():
    clock = Clock()
    fake = FakeReranker(clock, [9.0, 0.1, 0.1])
    assert probe.time_rerank(fake, "q", ["a"], runs=2, warmup=1, clock=clock)["max_s"] == 0.1


def test_worst_case_passages_fill_the_token_window():
    """Each synthetic passage is longer than the model's whole window, so every pair is cut at MAX_TOKENS (the slowest case)."""
    from semigraph.retrieval.rerank import MAX_TOKENS

    passages = probe.worst_case_passages(35)
    assert len(passages) == 35 and len(set(passages)) == 35
    assert all(len(p.split()) > MAX_TOKENS for p in passages)


def test_served_workload_comes_from_the_served_config():
    from semigraph.retrieval import fusion

    assert probe.served_workload() == (fusion.RERANK_DEPTH, fusion.RERANK_SERVED_CANDIDATES, fusion.RERANK_SERVED_TOKENS) == (20, 25, 192)


def test_worst_case_passages_follow_the_window_override():
    assert all(len(p.split()) > 1000 for p in probe.worst_case_passages(3, max_tokens=1000))


def test_overrides_change_the_window_the_reranker_is_loaded_at_and_the_larger_size(monkeypatch, capsys):
    clock, loaded = Clock(), []
    monkeypatch.setattr(probe, "load_reranker", lambda max_tokens=None: loaded.append(max_tokens) or FakeReranker(clock, [0.1] * 100))
    monkeypatch.setattr(probe.time, "perf_counter", clock)
    monkeypatch.setattr(probe, "cpu_stat", lambda path=probe.CPU_STAT: None)
    assert probe.main(["--runs", "1", "--warmup", "0", "--max-tokens", "128", "--max-candidates", "30"]) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert loaded == [128] and report["max_tokens"] == 128 and [r["candidates"] for r in report["results"]] == [20, 30]


def test_the_default_run_loads_the_served_window(monkeypatch, capsys):
    clock, loaded = Clock(), []
    monkeypatch.setattr(probe, "load_reranker", lambda max_tokens=None: loaded.append(max_tokens) or FakeReranker(clock, [0.1] * 100))
    monkeypatch.setattr(probe.time, "perf_counter", clock)
    monkeypatch.setattr(probe, "cpu_stat", lambda path=probe.CPU_STAT: None)
    probe.main(["--runs", "1", "--warmup", "0"])
    assert loaded == [192]


def test_overrides_must_be_positive():
    with pytest.raises(SystemExit):
        probe.main(["--max-tokens", "0"])
    with pytest.raises(SystemExit):
        probe.main(["--max-candidates", "0"])


def test_cpu_stat_reads_throttling_and_tolerates_a_missing_file(tmp_path):
    stat = tmp_path / "cpu.stat"
    stat.write_text("usage_usec 100\nnr_periods 10\nnr_throttled 4\nthrottled_usec 2500\n", encoding="utf-8")
    assert probe.cpu_stat(stat) == {"usage_usec": 100, "nr_periods": 10, "nr_throttled": 4, "throttled_usec": 2500}
    assert probe.cpu_stat(tmp_path / "absent") is None


def test_verdict_against_the_two_second_gate():
    assert probe.verdict([{"p95_s": 0.8}, {"p95_s": 2.0}]) == "PASS"
    assert probe.verdict([{"p95_s": 0.8}, {"p95_s": 2.01}]) == "FAIL"


def test_main_exits_2_when_the_model_does_not_load(monkeypatch, capsys):
    monkeypatch.setattr(probe, "load_reranker", lambda max_tokens=None: None)
    assert probe.main(["--runs", "1"]) == 2
    assert "not loaded" in capsys.readouterr().out


def test_main_prints_one_json_report_and_exits_0_on_pass(monkeypatch, capsys):
    clock = Clock()
    monkeypatch.setattr(probe, "load_reranker", lambda max_tokens=None: FakeReranker(clock, [0.2] * 100))
    monkeypatch.setattr(probe.time, "perf_counter", clock)
    monkeypatch.setattr(probe, "cpu_stat", lambda path=probe.CPU_STAT: None)
    assert probe.main(["--runs", "3", "--warmup", "1"]) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report["gate_s"] == 2.0 and report["verdict"] == "PASS"
    assert [r["candidates"] for r in report["results"]] == [20, 25] and report["max_tokens"] == 192
    assert report["throttling"] is None


def test_main_exits_1_on_fail(monkeypatch, capsys):
    clock = Clock()
    monkeypatch.setattr(probe, "load_reranker", lambda max_tokens=None: FakeReranker(clock, [3.0] * 100))
    monkeypatch.setattr(probe.time, "perf_counter", clock)
    monkeypatch.setattr(probe, "cpu_stat", lambda path=probe.CPU_STAT: None)
    assert probe.main(["--runs", "2", "--warmup", "0"]) == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["verdict"] == "FAIL"


def test_the_probe_never_prints_settings_or_touches_the_database():
    source = (ROOT / "scripts" / "rerank_latency_probe.py").read_text(encoding="utf-8")
    assert "neo4j" not in source.lower() and "password" not in source.lower() and "get_driver" not in source


def test_the_cost_map_is_pinned_local_before_any_semigraph_import():
    """On the production machine LiteLLM (pulled in by semigraph.retrieval) must not fetch its price table from the internet."""
    import os
    import subprocess
    import sys

    code = ("import importlib.util, os, sys\n"
            f"spec = importlib.util.spec_from_file_location('p', r'{ROOT / 'scripts' / 'rerank_latency_probe.py'}')\n"
            "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
            "print(os.environ.get('LITELLM_LOCAL_MODEL_COST_MAP'), any(m.startswith('semigraph') for m in sys.modules))\n")
    env = {k: v for k, v in os.environ.items() if k != "LITELLM_LOCAL_MODEL_COST_MAP"}
    done = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert done.stdout.split() == ["True", "False"], done.stderr[-1000:]
    source = (ROOT / "scripts" / "rerank_latency_probe.py").read_text(encoding="utf-8")
    assert source.index('"LITELLM_LOCAL_MODEL_COST_MAP"') < source.index("from semigraph")
    assert "23 MB" not in source and "305 MB" in source and "no upload in flight" in source

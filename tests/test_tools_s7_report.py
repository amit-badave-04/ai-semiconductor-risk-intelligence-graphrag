"""tools/s7/report.py and tools/s7/evidence.py: the S7 verdict from the raw files of a replay run (M5a I5).

The fixtures write run directories with EXACT distributions (``exact_values``: the nearest-rank p95 and p99 of the samples in the
judged window are the numbers asked for), so each pre-registered limit is tested at, just under and just over its boundary, one
at a time: only the criterion under test may change its verdict. Nothing here touches a database; the replay that makes these
files is tested in ``tests/test_tools_s7.py``.

Time: every fixture level lives in January 1970 (wall clock 1,000,000 s) so that a log line or a machine event can be placed
inside, before or after a level's window without depending on today's date. An event counts for a level only if it falls in
``[wall_start, wall_end]``.
"""

import json
import math
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.s7 import limits, report  # noqa: E402


WALL0 = 1_000_000.0                       # an epoch second well before 2026: every fixture level lives in January 1970
N_PER_OP = 1000                           # samples per op in the judged window (a multiple of 100)


def exact_values(n, p95, p99):
    """``n`` values whose nearest-rank p95 is ``p95`` and p99 is ``p99``: ranks 1..0.94n below p95, then p95 up to rank
    0.99n - 1, then p99 from rank 0.99n (so the 99th percentile lands on it)."""
    top = n - math.ceil(0.99 * n) + 1
    mid = math.ceil(0.99 * n) - 1 - math.ceil(0.95 * n) + 1
    low = n - top - mid
    return [p95 * (0.5 + 0.4 * i / low) for i in range(low)] + [p95] * mid + [p99] * top


def stamp(wall):
    return datetime.fromtimestamp(wall, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_level(run_dir, phase, *, live_rate=5.0, mix_name="m5a", duration=100.0, judged=50.0, pre=(10.0, 20.0), writes=(30.0, 40.0),
                retrieval=(100.0, 110.0), errors=0, n=N_PER_OP, paid=None, reserved=0, cpu=0.3, lag=0.01, started=None,
                restart=None, evidence=True, wall_start=WALL0):
    """One level directory: exactly ``n`` samples of each of four ops inside the judged window, with the given p95 / p99."""
    d = run_dir / phase
    d.mkdir(parents=True)
    rng = random.Random(1)
    window_start = duration - judged
    rows = []
    for op, (p95, p99) in (("cache_read", pre), ("reserve", pre), ("settle", writes), ("retrieval", retrieval)):
        values = exact_values(n, p95, p99)
        rng.shuffle(values)
        rows += [[round(window_start + judged * (i + 0.5) / n, 4), op, v, v, "ok"] for i, v in enumerate(values)]
    for k in range(errors):
        rows.append([round(duration - 2 - k * 0.01, 4), "reserve", 5.0, 5.0, "err:ServiceUnavailable"])
    (d / "samples.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    live = int(live_rate * duration) if started is None else started
    paid = live if paid is None else paid
    (d / "level.json").write_text(json.dumps({
        "phase": phase, "mix": mix_name, "backend": "inprocess", "live_rate": live_rate, "duration_s": duration, "judged_s": judged,
        "wall_start": wall_start, "wall_end": wall_start + duration,
        "asks": {"live_started": live, "cached_started": int(live * 0.8)}, "errors": errors, "denied": 0,
        "expected": {"paid_rows": live, "cached_rows": int(live * 0.8)},
        "driver": {"cpu_max": cpu, "cpu_p95": cpu}, "lag": {"p99_s": lag, "max_s": lag}}), encoding="utf-8")
    (d / "ledger_rows.json").write_text(json.dumps({"prefix": "s7:x", "paid_rows": paid, "reserved_rows": reserved,
                                                    "cached_rows": int(live * 0.8)}), encoding="utf-8")
    uptimes = [{"t": float(k), "ok": True, "ms": 3.0, "uptime_ms": 10_000 + k * 1000} for k in range(0, int(duration), 10)]
    if restart is not None:
        uptimes = [{**u, "uptime_ms": u["uptime_ms"] if u["t"] < restart else 500 + int(u["t"] - restart) * 1000} for u in uptimes]
    if evidence:
        (d / "server.jsonl").write_text("\n".join(json.dumps(u) for u in uptimes) + "\n", encoding="utf-8")
    return d


def good_run(tmp_path, **override):
    run_dir = tmp_path / "run"
    write_level(run_dir, "baseline", live_rate=0.5, duration=100.0, judged=100.0, retrieval=(100.0, 110.0))
    write_level(run_dir, "L5", **override)
    return run_dir


def verdict_of(run_dir, level="L5", **kwargs):
    doc = report.build(run_dir, **kwargs)
    return doc["levels"][level]["verdict"], doc


def test_the_fixture_distributions_land_exactly_on_the_asked_percentiles():
    for p95, p99 in ((10.0, 20.0), (50.0, 200.0), (100.0, 100.0), (400.0, 500.0)):
        values = exact_values(N_PER_OP, p95, p99)
        assert len(values) == N_PER_OP
        assert report.percentile(values, 0.95) == p95 and report.percentile(values, 0.99) == p99


def test_a_run_inside_every_limit_passes_and_the_report_says_which_figures(tmp_path):
    verdict, doc = verdict_of(good_run(tmp_path))
    assert verdict["result"] == "PASS" and verdict["reasons"] == []
    crit = {c["id"]: c for c in doc["levels"]["L5"]["criteria"]}
    assert {"pre_stream_p95", "pre_stream_p99", "writes_p95", "retrieval_vs_baseline", "error_rate", "lost_rows",
            "neo4j_restart", "neo4j_oom"} <= set(crit)
    assert all(c["passed"] is True for c in crit.values())
    assert doc["m5a"]["result"] == "PASS" and doc["m5a"]["level"] == 5
    assert doc["levels"]["L5"]["groups"]["pre_stream"]["n"] == 2 * N_PER_OP
    assert crit["pre_stream_p95"]["observed"] == 10.0 and crit["retrieval_vs_baseline"]["limit"] == 150.0
    json.dumps(doc, allow_nan=False)                                         # strict JSON: no NaN anywhere


@pytest.mark.parametrize("override, failed", [
    ({"pre": (60.0, 100.0)}, "pre_stream_p95"),
    ({"pre": (10.0, 250.0)}, "pre_stream_p99"),
    ({"writes": (400.0, 500.0)}, "writes_p95"),
    ({"retrieval": (200.0, 260.0)}, "retrieval_vs_baseline"),
    ({"errors": 30}, "error_rate"),
    ({"paid": 1999}, "lost_rows"),
    ({"reserved": 3}, "lost_rows"),
    ({"restart": 60.0}, "neo4j_restart"),
])
def test_each_pre_registered_limit_fails_the_level_on_its_own(tmp_path, override, failed):
    verdict, doc = verdict_of(good_run(tmp_path, **override))
    assert verdict["result"] == "FAIL", doc["levels"]["L5"]["criteria"]
    assert any(failed in reason for reason in verdict["reasons"]), verdict
    crit = {c["id"]: c for c in doc["levels"]["L5"]["criteria"]}
    assert crit[failed]["passed"] is False
    assert [c["id"] for c in crit.values() if c["passed"] is False] == [failed]


def test_the_limits_are_inclusive_at_the_boundary(tmp_path):
    verdict, doc = verdict_of(good_run(tmp_path, pre=(50.0, 200.0), writes=(100.0, 100.0), retrieval=(150.0, 150.0)))
    assert verdict["result"] == "PASS", doc["levels"]["L5"]["criteria"]


def test_a_hair_over_the_boundary_fails(tmp_path):
    verdict, _ = verdict_of(good_run(tmp_path, pre=(50.01, 200.0)))
    assert verdict["result"] == "FAIL" and any("pre_stream_p95" in r for r in verdict["reasons"])


def test_only_the_judged_window_counts_early_slowness_is_ignored(tmp_path):
    run_dir = good_run(tmp_path)
    path = run_dir / "L5" / "samples.jsonl"
    early = [[1.0 + i * 0.01, "reserve", 900.0, 900.0, "ok"] for i in range(200)]
    path.write_text(path.read_text(encoding="utf-8") + "\n".join(json.dumps(r) for r in early) + "\n", encoding="utf-8")
    assert verdict_of(run_dir)[0]["result"] == "PASS"


def test_errors_are_a_rate_over_the_judged_samples_and_excluded_from_the_latency_percentiles(tmp_path):
    verdict, doc = verdict_of(good_run(tmp_path, errors=1))
    crit = {c["id"]: c for c in doc["levels"]["L5"]["criteria"]}
    assert crit["error_rate"]["observed"] == pytest.approx(1 / (4 * N_PER_OP + 1)) and crit["error_rate"]["observed"] < limits.ERROR_RATE_MAX
    assert verdict["result"] == "PASS"
    assert doc["levels"]["L5"]["groups"]["pre_stream"]["n"] == 2 * N_PER_OP           # the failed reserve is not a latency sample


@pytest.mark.parametrize("override, tag", [({"cpu": 0.8}, "driver_cpu"), ({"started": 100}, "offered_rate"),
                                           ({"lag": 2.5}, "arrival_lag")])
def test_a_driver_that_could_not_deliver_the_load_voids_the_level_before_any_gate(tmp_path, override, tag):
    verdict, doc = verdict_of(good_run(tmp_path, pre=(500.0, 900.0), **override))
    assert verdict["result"] == "VOID" and any(tag in r for r in verdict["reasons"])


def test_missing_evidence_is_incomplete_never_a_pass(tmp_path):
    verdict, doc = verdict_of(good_run(tmp_path, evidence=False))
    assert verdict["result"] == "INCOMPLETE" and any("neo4j_restart" in r for r in verdict["reasons"])
    crit = {c["id"]: c for c in doc["levels"]["L5"]["criteria"]}
    assert crit["neo4j_restart"]["passed"] is None and crit["neo4j_oom"]["passed"] is None


def test_a_missing_baseline_leaves_the_retrieval_criterion_unjudged(tmp_path):
    run_dir = tmp_path / "run"
    write_level(run_dir, "L5")
    verdict, doc = verdict_of(run_dir)
    crit = {c["id"]: c for c in doc["levels"]["L5"]["criteria"]}
    assert crit["retrieval_vs_baseline"]["passed"] is None and crit["retrieval_vs_baseline"]["limit"] is None
    assert verdict["result"] == "INCOMPLETE" and doc["baseline"]["retrieval_p95_ms"] is None
    json.dumps(doc, allow_nan=False)


def test_the_baseline_is_judged_after_its_warm_up_and_the_control_is_reported_not_judged(tmp_path):
    run_dir = tmp_path / "run"
    write_level(run_dir, "baseline", live_rate=0.5, duration=900.0, judged=600.0, retrieval=(100.0, 110.0))
    path = run_dir / "baseline" / "samples.jsonl"
    cold = [[1.0 + i * 0.1, "retrieval", 5000.0, 5000.0, "ok"] for i in range(500)]            # the first 5 minutes
    path.write_text(path.read_text(encoding="utf-8") + "\n".join(json.dumps(r) for r in cold) + "\n", encoding="utf-8")
    write_level(run_dir, "L5")
    write_level(run_dir, "control", mix_name="today", judged=50.0)
    doc = report.build(run_dir)
    assert doc["baseline"]["retrieval_p95_ms"] == 100.0                               # the cold start did not move it
    assert doc["levels"]["control"]["verdict"]["result"] == "INFORMATION" and "criteria" not in doc["levels"]["control"]
    assert doc["levels"]["baseline"]["verdict"]["result"] == "INFORMATION"
    assert doc["m5a"]["result"] == "PASS"


def level_wall(phase_dir):
    meta = json.loads((phase_dir / "level.json").read_text(encoding="utf-8"))
    return meta["wall_start"], meta["wall_end"]


def snapshot_of(tmp_path, log_lines, events=()):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "logs-semigraph-neo4j-stg.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    (evidence / "machines-semigraph-neo4j-stg.json").write_text(json.dumps([{"id": "m1", "events": list(events)}]), encoding="utf-8")
    return evidence


def test_oom_and_restart_are_read_from_the_snapshot_logs_and_machine_events(tmp_path):
    run_dir = good_run(tmp_path)
    start, end = level_wall(run_dir / "L5")
    evidence = snapshot_of(tmp_path, [
        f"{stamp(start - 3600)} app[m1] sin [info]INFO  Started.",                                  # before the level
        f"{stamp(start + 50)} app[m1] sin [error]java.lang.OutOfMemoryError: Java heap space",      # inside it
        "    at java.base/java.util.Arrays.copyOf(Arrays.java:3481)"])
    verdict, doc = verdict_of(run_dir, evidence_dir=evidence)
    crit = {c["id"]: c for c in doc["levels"]["L5"]["criteria"]}
    assert crit["neo4j_oom"]["passed"] is False and verdict["result"] == "FAIL"
    assert crit["neo4j_restart"]["passed"] is True                    # the only "Started." is before the level began
    assert [c["id"] for c in crit.values() if c["passed"] is False] == ["neo4j_oom"]


def test_a_start_line_inside_the_level_is_a_restart(tmp_path):
    run_dir = good_run(tmp_path)
    start, _ = level_wall(run_dir / "L5")
    evidence = snapshot_of(tmp_path, [f"{stamp(start + 20)} app[m1] sin [info]INFO  Started."])
    verdict, doc = verdict_of(run_dir, evidence_dir=evidence)
    crit = {c["id"]: c for c in doc["levels"]["L5"]["criteria"]}
    assert crit["neo4j_restart"]["passed"] is False and crit["neo4j_oom"]["passed"] is True


def test_an_oom_during_another_level_does_not_fail_this_one(tmp_path):
    run_dir = good_run(tmp_path)
    write_level(run_dir, "L20", live_rate=20.0, wall_start=WALL0 + 400)
    evidence = snapshot_of(tmp_path, [f"{stamp(WALL0 + 450)} app[m1] sin [error]java.lang.OutOfMemoryError: Java heap space"])
    doc = report.build(run_dir, evidence_dir=evidence)
    assert doc["levels"]["L5"]["verdict"]["result"] == "PASS"
    assert doc["levels"]["L20"]["verdict"]["result"] == "FAIL"
    assert doc["m5a"]["result"] == "PASS"                                # M5a asks only for level 5


def test_a_machine_exit_event_that_says_oom_killed_fails_the_level(tmp_path):
    run_dir = good_run(tmp_path)
    start, _ = level_wall(run_dir / "L5")
    event = {"type": "exit", "timestamp": int((start + 30) * 1000), "request": {"exit_event": {"exit_code": 137, "oom_killed": True}}}
    evidence = snapshot_of(tmp_path, [f"{stamp(start - 60)} app[m1] sin [info]INFO  ready"], [event])
    crit = {c["id"]: c for c in verdict_of(run_dir, evidence_dir=evidence)[1]["levels"]["L5"]["criteria"]}
    assert crit["neo4j_oom"]["passed"] is False


def test_a_memory_error_status_in_the_samples_is_an_oom(tmp_path):
    run_dir = good_run(tmp_path)
    path = run_dir / "L5" / "samples.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + json.dumps([60.0, "retrieval", 9.0, 9.0, "err:MemoryPoolOutOfMemoryError"]) + "\n",
                    encoding="utf-8")
    crit = {c["id"]: c for c in verdict_of(run_dir)[1]["levels"]["L5"]["criteria"]}
    assert crit["neo4j_oom"]["passed"] is False


def test_a_log_that_starts_after_the_level_cannot_prove_the_absence_of_anything(tmp_path):
    """``flyctl logs --no-tail`` is a short buffer: a snapshot taken at the end of a long window does not reach back to this level."""
    run_dir = good_run(tmp_path)
    _, end = level_wall(run_dir / "L5")
    evidence = snapshot_of(tmp_path, [f"{stamp(end + 600)} app[m1] sin [info]INFO  all quiet"],
                           [{"type": "start", "timestamp": int((end + 700) * 1000)}])
    verdict, doc = verdict_of(run_dir, evidence_dir=evidence)
    crit = {c["id"]: c for c in doc["levels"]["L5"]["criteria"]}
    for cid in ("neo4j_restart", "neo4j_oom"):
        assert crit[cid]["passed"] is True and crit[cid]["sources"] == ["probes"], crit[cid]      # the probes, not the log
        assert "do not reach back" in crit[cid]["detail"]
    assert verdict["result"] == "PASS"


def test_without_probes_a_log_that_does_not_reach_the_level_leaves_it_unjudged(tmp_path):
    run_dir = good_run(tmp_path, evidence=False)                                       # no server.jsonl
    _, end = level_wall(run_dir / "L5")
    evidence = snapshot_of(tmp_path, [f"{stamp(end + 600)} app[m1] sin [info]INFO  all quiet"])
    verdict, doc = verdict_of(run_dir, evidence_dir=evidence)
    crit = {c["id"]: c for c in doc["levels"]["L5"]["criteria"]}
    assert crit["neo4j_restart"]["passed"] is None and crit["neo4j_oom"]["passed"] is None
    assert verdict["result"] == "INCOMPLETE"


def test_a_log_that_reaches_back_to_the_start_of_the_level_is_a_source_of_the_absence(tmp_path):
    run_dir = good_run(tmp_path, evidence=False)
    start, _ = level_wall(run_dir / "L5")
    evidence = snapshot_of(tmp_path, [f"{stamp(start - 60)} app[m1] sin [info]INFO  all quiet"],
                           [{"type": "start", "timestamp": int((start - 3600) * 1000)}])
    verdict, doc = verdict_of(run_dir, evidence_dir=evidence)
    crit = {c["id"]: c for c in doc["levels"]["L5"]["criteria"]}
    assert crit["neo4j_restart"]["passed"] is True and crit["neo4j_restart"]["sources"] == ["logs", "events"]
    assert crit["neo4j_oom"]["passed"] is True and verdict["result"] == "PASS"


def test_a_snapshot_per_level_is_read_for_its_own_level(tmp_path):
    """The advice is one snapshot after each level (a recent-lines buffer does not reach back): ``EVIDENCE/<phase>/``."""
    run_dir = good_run(tmp_path)
    write_level(run_dir, "L10", live_rate=10.0, wall_start=WALL0 + 400)
    root = tmp_path / "evidence"
    for phase, wall, line in (("L5", WALL0, "java.lang.OutOfMemoryError: Java heap space"), ("L10", WALL0 + 400, "all quiet")):
        folder = root / phase
        folder.mkdir(parents=True)
        lines = [f"{stamp(wall - 30)} app[m1] sin [info]INFO  warm", f"{stamp(wall + 40)} app[m1] sin [error]{line}"]
        (folder / "logs-semigraph-neo4j-stg.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    doc = report.build(run_dir, evidence_dir=root)
    assert doc["levels"]["L5"]["verdict"]["result"] == "FAIL" and doc["levels"]["L10"]["verdict"]["result"] == "PASS"
    assert "logs" in {c["id"]: c for c in doc["levels"]["L10"]["criteria"]}["neo4j_oom"]["sources"]


def test_a_level_reports_the_size_of_the_tables_it_ran_on(tmp_path):
    run_dir = good_run(tmp_path)
    meta_path = run_dir / "L5" / "level.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["table_counts"] = {"start": {"svc_query": 120, "svc_answer": 80}, "end": {"svc_query": 5000, "svc_answer": 1500}}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert report.build(run_dir)["levels"]["L5"]["table_counts"]["start"] == {"svc_query": 120, "svc_answer": 80}


def test_report_main_writes_s7_json_and_exits_by_the_required_level(tmp_path, capsys):
    run_dir = good_run(tmp_path)
    out = tmp_path / "s7.json"
    assert report.main([str(run_dir), "--out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["m5a"]["result"] == "PASS" and "L5" in doc["levels"]
    bad = tmp_path / "bad"
    write_level(bad, "L5", errors=50)
    assert report.main([str(bad), "--out", str(tmp_path / "b.json")]) == 1
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    assert report.main([str(nothing), "--out", str(tmp_path / "n.json")]) == 1                  # no L5 at all is not a pass
    assert json.loads((tmp_path / "n.json").read_text(encoding="utf-8"))["m5a"]["result"] == "NOT_RUN"


def test_percentiles_are_nearest_rank_and_an_empty_sample_is_none():
    values = list(range(1, 101))
    assert report.percentile(values, 0.95) == 95 and report.percentile(values, 0.99) == 99 and report.percentile([7], 0.95) == 7
    assert report.percentile([], 0.95) is None
    assert report.percentile(list(range(1, 101)), 0.07) == 7                    # 0.07 * 100 is 7.000000000000001 in floats


def test_a_driver_crash_voids_the_level(tmp_path):
    run_dir = good_run(tmp_path)
    meta_path = run_dir / "L5" / "level.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["driver_errors"] = 2
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    verdict, _ = verdict_of(run_dir)
    assert verdict["result"] == "VOID" and any("driver_errors" in r for r in verdict["reasons"])



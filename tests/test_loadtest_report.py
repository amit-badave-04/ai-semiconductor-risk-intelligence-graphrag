"""scripts/loadtest_report.py: every branch of the PASS / FAIL / VOID rule, from fixture runs written to disk.

A ``RunBuilder`` writes a synthetic run directory (two generator logs with their own clocks, cpu_watch files for the
generators, the mock and the API machine, the ledger snapshot, the mock counter, the offline salt check) that PASSES by
default: 2.7 live asks/s offered for 200 s, TTFE p95 0.66 s, no error, ledger == terminals, CPU well inside the limits.
Each test changes ONE thing and checks the verdict, the reason text and the exit code. Nothing here imports locust.
"""

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.loadtest import model, salt  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


report = _load("loadtest_report", ROOT / "scripts" / "loadtest_report.py")

T0 = 1_000_000.0
POOL_SHA = "p" * 64
FLY_EMBED_S = 0.40


class RunBuilder:
    """Writes one run directory. Every knob defaults to a healthy run; the counts it produced are on the instance."""

    def __init__(self, root: Path, **kw):
        self.root = root
        self.opt = dict(
            run_id="s2-fixture", workers=2, steady_s=100, soak_s=100, ramp_s=30, fault_s=30, live_rate=2.7, cached_rate=2.2,
            ttfe_base=0.20, ttfe_slow_fraction=0.0, ttfe_slow=2.0, shed=0, shed_status=429, drops=0, error_events=0, extra_errors=0,
            gen_cpu=40.0, mock_cpu=20.0, server_cpu=45.0, server_cpu_hot_fraction=0.0, server_cpu_hot=80.0, server_cpu_s_per_s=1.2,
            rss_pct=30.0, concurrent_per_worker=21, salt_ok=True, salt_pool=POOL_SHA, local_graph_ran=True, fly_embed_s=FLY_EMBED_S,
            ledger=True, ledger_delta=None, unsettled=0, cap=None, spike_s=0, outside_cpu=None, outside_server_rate=None, live_cache_hits=0, restart_worker=False, iterations=0, overran=0, mock_requests=None, params=None, shape=True, vus=1000, upload_vus=5,
            generator_cpu_files=2, mock_cpu_file=True, server_cpu_file=True, salt_file=True, meta_file=True, unsalted_live=0,
            duplicate_salt=False, other_phases=True, fault_5xx=0, uploads_ready=3, uploads_failed=0, seq_offset=0, skew=0.37)
        self.opt.update(kw)
        self.counts = {}

    # -- the generator logs ----------------------------------------------------------------------------------------------

    def _phases(self):
        o = self.opt
        out = [("ramp", o["ramp_s"])] if o["other_phases"] else []
        out += [("steady", o["steady_s"])] + ([("spike", o["spike_s"])] if o["spike_s"] else []) + [("soak", o["soak_s"])]
        return out + ([("fault", o["fault_s"])] if o["other_phases"] else [])

    def _is_measured_second(self, s: int) -> bool:
        """Is second ``s`` of the run (generator-0 clock) inside steady or soak?"""
        t = 0
        for phase, length in self._phases():
            if t <= s < t + length:
                return phase in ("steady", "soak")
            t += length
        return False

    def write(self) -> Path:
        o, root = self.opt, self.root
        (root / "generator").mkdir(parents=True, exist_ok=True)
        self.live_admitted = Counter()
        self.cached_done = 0
        self.requests_measured = 0
        self.live_measured = 0
        for w in range(o["workers"]):
            self._write_worker(w)
        self._cpu_files()
        self._ledger_mock_salt_meta()
        return root

    def _write_worker(self, w: int) -> None:
        o = self.opt
        t = T0 + w * o["skew"]
        lines, live_acc, cached_acc = [], 0.0, 0.0
        n_salt, live_i = o["seq_offset"], 0
        budget = {"shed": o["shed"] // o["workers"] + (w < o["shed"] % o["workers"]),
                  "drops": o["drops"] // o["workers"] + (w < o["drops"] % o["workers"]),
                  "error_events": o["error_events"] // o["workers"] + (w < o["error_events"] % o["workers"]),
                  "extra_errors": o["extra_errors"] // o["workers"] + (w < o["extra_errors"] % o["workers"]),
                  "fault_5xx": o["fault_5xx"] if w == 0 else 0, "cache_hits": o["live_cache_hits"] if w == 0 else 0}
        for phase, length in self._phases():
            lines.append({"kind": "phase", "name": phase, "ts": t, "run_id": o["run_id"], "worker": w})
            measured = phase in ("steady", "soak")
            for sec in range(length):
                ts = t + sec
                lines.append({"kind": "streams", "phase": phase, "t_phase": sec, "live_in_flight": o["concurrent_per_worker"],
                              "total_in_flight": o["concurrent_per_worker"] + 3, "ts": ts, "run_id": o["run_id"], "worker": w})
                live_acc += o["live_rate"] / o["workers"]
                cached_acc += o["cached_rate"] / o["workers"]
                for _ in range(int(live_acc)):
                    lines += self._live_ask(w, phase, ts, live_i, n_salt, budget, measured)
                    live_i += 1
                    n_salt += 1
                live_acc -= int(live_acc)
                for _ in range(int(cached_acc)):
                    lines += self._cached_ask(w, phase, ts, measured)
                cached_acc -= int(cached_acc)
            t += length
        lines.append({"kind": "streams", "phase": self._phases()[-1][0], "t_phase": length, "live_in_flight": 0, "total_in_flight": 0,
                      "ts": t, "run_id": o["run_id"], "worker": w})
        for k in range(o["iterations"] if w == 0 else 0):
            lines.append({"kind": "iteration", "phase": "steady", "ts": T0 + 40, "run_id": o["run_id"], "worker": 0, "n": k,
                          "overran": k < o["overran"]})
        if w == 0:
            for _ in range(o["uploads_ready"]):
                lines.append({"kind": "upload", "outcome": "ready", "phase": "steady", "ts": T0 + 50, "run_id": o["run_id"], "worker": 0})
            for _ in range(o["uploads_failed"]):
                lines.append({"kind": "upload", "outcome": "failed", "phase": "steady", "ts": T0 + 51, "run_id": o["run_id"], "worker": 0})
        path = self.root / "generator" / f"events.w{w}.p{100 + w}.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        if o["restart_worker"] and w == 0:                                    # the same worker digit, a second process
            (self.root / "generator" / "events.w0.p999.jsonl").write_text(
                json.dumps({"kind": "phase", "name": "ramp", "ts": T0 + 90, "run_id": o["run_id"], "worker": 0}) + "\n", encoding="utf-8")

    def _read_records(self, w, phase, ts, measured, n=4):
        out = [{"kind": "read", "label": "stats", "status": 200, "outcome": "ok", "phase": phase, "ts": ts, "run_id": self.opt["run_id"],
                "worker": w, "vu": 1} for _ in range(n)]
        self.requests_measured += n if measured else 0
        return out

    def _ask(self, w, phase, ts, klass, **fields):
        base = {"kind": "ask", "klass": klass, "strategy": "hybrid", "phase": phase, "ts": ts, "run_id": self.opt["run_id"],
                "worker": w, "vu": 1, "ip": "10.0.0.1", "status": 200, "outcome": "done", "cached": klass == "cached", "salt": None}
        return {**base, **fields}

    def _live_ask(self, w, phase, ts, i, n_salt, budget, measured):
        o = self.opt
        question = salt.apply_salt(f"What does item {w}-{i} say about supply risk?", w, n_salt % 1_000_000)
        if o["duplicate_salt"] and i % 10 == 9:
            question = salt.apply_salt("What does item dup say about supply risk?", w, 7)
        if i < o["unsalted_live"] and w == 0:
            question = f"What does item {w}-{i} say about supply risk?"
        fields = {"question": question, "salt": f"{w}{n_salt % 1_000_000:06d}", "ttfe_s": o["ttfe_base"] + 0.02 * (i % 25)}
        if o["ttfe_slow_fraction"] and i % round(1 / o["ttfe_slow_fraction"]) == 0:
            fields["ttfe_s"] = o["ttfe_slow"]
        recs = self._read_records(w, phase, ts, measured)
        if measured and budget["shed"] > 0:
            budget["shed"] -= 1
            fields.update(status=o["shed_status"], outcome="shed_429" if o["shed_status"] == 429 else "shed_503", ttfe_s=None)
        elif measured and budget["drops"] > 0:
            budget["drops"] -= 1
            fields.update(outcome="dropped", ttfe_s=None)
        elif measured and budget["error_events"] > 0:
            budget["error_events"] -= 1
            fields.update(outcome="error_event")
        elif phase == "fault" and budget["fault_5xx"] > 0:
            budget["fault_5xx"] -= 1
            fields.update(status=500, outcome="http_500", ttfe_s=None)
        if measured and budget["extra_errors"] > 0:
            budget["extra_errors"] -= 1
            recs[0].update(status=500, outcome="http_500")
        if phase == "ramp":                                                    # heavy errors OUTSIDE the measured phases
            fields.update(status=429, outcome="shed_429", ttfe_s=None)
        if measured and budget["cache_hits"] > 0:
            budget["cache_hits"] -= 1
            fields.update(cached=True)                                        # a salted ask the server answered from its cache
        ask = self._ask(w, phase, ts, "live_pool", **fields)
        if ask["status"] == 200 and ask["cached"] is not True:
            self.live_admitted[ask["outcome"]] += 1
        if measured:
            self.requests_measured += 1
            self.live_measured += 1
        return recs + [ask]

    def _cached_ask(self, w, phase, ts, measured):
        if measured:
            self.requests_measured += 1
            self.cached_done += 1
        return self._read_records(w, phase, ts, measured) + [
            self._ask(w, phase, ts, "cached", question="What was Nvidia's total revenue for fiscal 2024?", ttfe_s=0.01)]

    # -- the other inputs -------------------------------------------------------------------------------------------------

    def _cpu_files(self):
        o = self.opt
        (self.root / "cpu").mkdir(exist_ok=True)
        span = sum(length for _, length in self._phases())

        def write(name, role, fn):
            rows = []
            for s in range(-5, span + 6):
                row = {"t": T0 + s, "name": name, "role": role, "ncores": 2, "n_procs": 1, **fn(s)}
                if o["outside_cpu"] is not None and not self._is_measured_second(s):
                    row.update(cpu_pct_system=o["outside_cpu"], cpu_pct_gate=o["outside_cpu"], proc_pct_core_max=o["outside_cpu"])
                rows.append(row)
            (self.root / "cpu" / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

        for g in range(o["generator_cpu_files"]):
            write(f"gen-{g}", "generator", lambda s, g=g: {"cpu_pct_system": o["gen_cpu"], "cpu_pct_gate": o["gen_cpu"], "cpu_time_s": s * 0.5,
                                                          "rss_pct": 10.0, "proc_pct_core_max": o["gen_cpu"], "cpu_pct_procs": o["gen_cpu"] / 2})
        if o["mock_cpu_file"]:
            write("mock", "mock", lambda s: {"cpu_pct_system": o["mock_cpu"], "cpu_pct_gate": o["mock_cpu"], "cpu_time_s": s * 0.1,
                                             "rss_pct": 5.0, "proc_pct_core_max": o["mock_cpu"], "cpu_pct_procs": o["mock_cpu"] / 2})
        if o["server_cpu_file"]:
            hot = o["server_cpu_hot_fraction"]

            def cumulative(s):
                """Process CPU-seconds: server_cpu_s_per_s per measured second, outside_server_rate per other second."""
                total = 50.0
                for u in range(-5, s):
                    outside = o["outside_server_rate"] is not None and not self._is_measured_second(u)
                    total += o["outside_server_rate"] if outside else o["server_cpu_s_per_s"]
                return total

            def server(s):
                pct = o["server_cpu_hot"] if hot and (s % round(1 / hot)) == 0 else o["server_cpu"]
                return {"cpu_pct_system": pct, "cpu_pct_gate": pct, "cpu_time_s": cumulative(s),
                        "rss_pct": o["rss_pct"], "proc_pct_core_max": pct, "cpu_pct_procs": pct}
            write("api", "server", server)

    def _ledger_mock_salt_meta(self):
        o, root = self.opt, self.root
        mapping = {"done": "done", "error_event": "error", "dropped": "abandoned"}
        rows = {}
        for outcome, n in self.live_admitted.items():
            rows[mapping.get(outcome, "abandoned")] = rows.get(mapping.get(outcome, "abandoned"), 0) + n
        for key, delta in (o["ledger_delta"] or {}).items():
            rows[key] = rows.get(key, 0) + delta
        total_admitted = sum(self.live_admitted.values())
        self.admitted = total_admitted
        if o["ledger"]:
            (root / "ledger.json").write_text(json.dumps({"scope": "run", "paid_rows_by_outcome": rows, "unsettled_rows": o["unsettled"],
                                                           "cached_rows": self.cached_done + (0 if o["other_phases"] is False else 0), "cap": o["cap"]}), "utf-8")
        mock = o["mock_requests"] if o["mock_requests"] is not None else total_admitted
        (root / "mock.json").write_text(json.dumps({"requests": mock}), "utf-8")
        if o["salt_file"]:
            (root / "salt_check.json").write_text(json.dumps({
                "ok": o["salt_ok"], "pool_sha256": o["salt_pool"], "salt_format": salt.SALT_FORMAT,
                "local_graph": {"requested": o["local_graph_ran"], "ran": o["local_graph_ran"]}}), "utf-8")
        for w in range(o["workers"]):
            params = {"think_min_s": 120.0, "think_max_s": 300.0, "upload_period_s": 600.0, "static_paths": [], "mix": dict(model.ASK_MIX),
                      "p_evidence": 0.2, "p_dossier": 0.05}
            params.update(o["params"] or {})
            (root / "generator" / f"meta.w{w}.p{100 + w}.json").write_text(json.dumps({
                "kind": "meta", "run_id": o["run_id"], "worker": w, "params": params, "vus": o["vus"], "upload_vus": o["upload_vus"],
                "shape": o["shape"], "salt_format": salt.SALT_FORMAT, "pool_sha256": POOL_SHA}), "utf-8")
        if o["meta_file"]:
            meta = {"run_id": o["run_id"], "window": {"quote": 1.25, "machines": ["performance-2x"], "hours": 3.5, "derived_usd": 1.2},
                    "fleet": {"api": "performance-2x"}, "settings": {"stats": {"limits": {"max_queries_per_day": 0}}}}
            if o["fly_embed_s"] is not None:
                meta["fly_embed_s"] = o["fly_embed_s"]
            (root / "meta.json").write_text(json.dumps(meta), "utf-8")


from collections import Counter  # noqa: E402


def run_report(tmp_path, **kw):
    builder = RunBuilder(tmp_path / "run", **kw)
    builder.write()
    return report.compute_report(builder.root), builder


def verdict(tmp_path, **kw):
    out, _ = run_report(tmp_path, **kw)
    return out["verdict"]["result"], out


# =====================================================================================================================
# PASS
# =====================================================================================================================

def test_a_healthy_run_passes_and_the_verdict_is_computed_from_the_files(tmp_path):
    result, out = verdict(tmp_path)
    assert result == "PASS" and out["verdict"]["void_reasons"] == [] and out["verdict"]["fail_reasons"] == []
    m = out["metrics"]
    assert m["offered_live_rate"] == pytest.approx(2.7, abs=0.01) and m["live_asks_sent"] == 540
    assert m["window_s"] == 200.0 and m["dropped"] == 0 and m["errors"] == 0 and m["error_rate"] == 0.0
    assert m["live_ttfe_p95"] == pytest.approx(0.66, abs=0.001) and m["cached_ttfe_p95"] == pytest.approx(0.01)
    assert m["errors_by_status"] == {} and m["max_sustained_live_rate"] == pytest.approx(2.7, abs=0.05)
    assert m["concurrent_live_max"] == 42 and m["concurrent_live_min"] == 42
    assert all(g["status"] == "pass" for g in out["gates"].values())


def test_the_run_json_has_the_planned_schema(tmp_path):
    _, out = verdict(tmp_path)
    for key in ("run_id", "window", "fleet", "settings", "phases", "metrics", "validity", "server", "verdict", "gates", "inputs",
                "reported_not_gated"):
        assert key in out, key
    for key in ("live_ttfe_p50", "live_ttfe_p95", "live_ttfe_p99", "cached_ttfe_p95", "concurrent_live_max", "dropped",
                "errors_by_status", "offered_live_rate", "max_sustained_live_rate", "first10min"):
        assert key in out["metrics"], key
    for key in ("distinct_salted", "ledger_rows_by_outcome", "generator_terminals_by_outcome", "mock_requests",
                "generator_cpu_max", "mock_cpu_max", "offline_salt_checks", "cpu_s_per_live_ask", "fly_embed_s"):
        assert key in out["validity"], key
    assert set(out["server"]) >= {"cpu_pct_p95", "rss_pct", "loop_lag_warnings", "limiter_at_capacity_s"}
    assert [p["name"] for p in out["phases"]] == ["ramp", "steady", "soak", "fault"]
    assert out["window"]["derived_usd"] == 1.2 and out["verdict"]["void_fail_split"] == "PENDING OWNER SIGN-OFF"
    json.dumps(out)                                                              # serializable: no inf, no NaN


def test_the_validity_numbers_are_the_measured_ones(tmp_path):
    _, out = verdict(tmp_path)
    v = out["validity"]
    assert v["distinct_salted"] == v["live_sends"] and v["live_without_salt"] == 0 and v["cached_with_salt"] == 0
    assert v["generator_cpu_max"] == {"gen-0": 40.0, "gen-1": 40.0} and v["mock_cpu_max"] == 20.0
    assert v["cpu_s_per_live_ask"] == pytest.approx(1.2 * 200 / 540, abs=0.01) and v["fly_embed_s"] == FLY_EMBED_S
    assert v["offline_salt_checks"] == {"ok": True, "local_graph_ran": True}
    assert v["ledger_rows_by_outcome"]["done"] == v["generator_terminals_by_outcome"]["done"]
    assert out["server"]["cpu_pct_p95"] == 45.0 and out["server"]["rss_pct"] == 30.0


def test_only_the_measured_phases_count_the_ramp_errors_are_excluded(tmp_path):
    result, out = verdict(tmp_path)                                              # the fixture's ramp is all 429s
    assert result == "PASS" and out["metrics"]["errors"] == 0
    wide, _ = run_report(tmp_path / "again")
    again = report.compute_report(tmp_path / "again" / "run", phases=("ramp", "steady", "soak"))
    assert again["metrics"]["errors"] > 0 and wide["metrics"]["errors"] == 0


def test_the_first_ten_minutes_block_is_the_cold_start_view(tmp_path):
    _, out = verdict(tmp_path)
    first = out["metrics"]["first10min"]
    assert first["minutes"] == 10 and first["error_rate"] == 0.0 and first["offered_live_rate"] == pytest.approx(2.7, abs=0.05)
    assert first["live_ttfe_p95"] == pytest.approx(0.66, abs=0.001)


def test_the_not_gated_criteria_are_reported_with_their_numbers(tmp_path):
    _, out = verdict(tmp_path, fault_5xx=2, uploads_failed=1)
    rng = out["reported_not_gated"]
    assert rng["concurrent_live_streams"] == {"max": 42, "p50": 42, "asked": 40}
    assert rng["rss_pct_max"] == 30.0 and rng["uploads"] == {"total": 4, "ready": 3, "by_outcome": {"ready": 3, "failed": 1}}
    assert rng["fault_phase"]["http_5xx"] == 2 and "NOT gated" in rng["note"]
    assert out["verdict"]["result"] == "PASS"                                    # a failed upload or a fault-phase 5xx does not change the verdict here


# =====================================================================================================================
# VOID: each clause alone
# =====================================================================================================================

def test_void_when_the_generator_offered_less_than_95_percent_of_2_6(tmp_path):
    result, out = verdict(tmp_path, live_rate=2.2)
    assert result == "VOID" and any("offered live rate" in r and "2.47" in r for r in out["verdict"]["void_reasons"])


@pytest.mark.parametrize("rate,expected", [(2.5, "PASS"), (2.46, "VOID")])
def test_the_void_floor_is_inclusive_at_2_47(tmp_path, rate, expected):
    # 2.47 / s exactly is allowed; a run offering 2.46 is not (floors scale with the window: 200 s here)
    assert verdict(tmp_path, live_rate=rate)[0] == expected


def test_void_when_a_generator_cpu_exceeded_70_percent(tmp_path):
    result, out = verdict(tmp_path, gen_cpu=75.0)
    assert result == "VOID" and sum("generator" in r and "75 %" in r for r in out["verdict"]["void_reasons"]) == 2


def test_a_generator_exactly_at_70_percent_is_not_void(tmp_path):
    assert verdict(tmp_path, gen_cpu=70.0)[0] == "PASS"


def test_void_when_the_mock_exceeded_50_percent(tmp_path):
    result, out = verdict(tmp_path, mock_cpu=55.0)
    assert result == "VOID" and any("mock" in r and "55 %" in r for r in out["verdict"]["void_reasons"])
    assert verdict(tmp_path / "edge", mock_cpu=50.0)[0] == "PASS"


def test_the_mock_cpu_rule_is_a_harness_rule_and_not_a_pre_registered_clause(tmp_path):
    """The 50 % threshold comes from the harness plan, not from M5_PLAN / M5_DECISIONS / council 5: the rule stays (a mock over
    50 % voids the run) but it is labelled as what it is, so a VOID that rests on it alone is reported as resting on no
    pre-registered clause, the same as any other clause the harness added. The generator's 70 % is a registered clause."""
    result, out = verdict(tmp_path / "mock", mock_cpu=55.0)
    clauses = out["verdict"]["void_clauses"]
    assert result == "VOID" and [c["source"] for c in clauses] == ["harness-added"]
    assert clauses[0]["text"].startswith(f"{model.HARNESS_RULE_LABEL}: mock ") and "55 %" in clauses[0]["text"]
    assert out["verdict"]["void_reasons"][0].startswith("[harness-added] harness rule (not pre-registered): mock ")
    assert out["verdict"]["void_without_a_pre_registered_clause"] is True
    assert "rests on no pre-registered clause" in report.render(out)

    result, out = verdict(tmp_path / "generator", gen_cpu=75.0)
    assert result == "VOID" and {c["source"] for c in out["verdict"]["void_clauses"]} == {"pre-registered"}
    assert not any(model.HARNESS_RULE_LABEL in r for r in out["verdict"]["void_reasons"])
    assert out["verdict"]["void_without_a_pre_registered_clause"] is False

    result, out = verdict(tmp_path / "both", mock_cpu=55.0, gen_cpu=75.0)                 # one registered clause is enough
    assert {c["source"] for c in out["verdict"]["void_clauses"]} == {"pre-registered", "harness-added"}
    assert out["verdict"]["void_without_a_pre_registered_clause"] is False


def test_the_label_of_the_mock_cpu_rule_is_the_same_wherever_the_rule_is_described():
    """The report's header, the traffic model's constant and the README must agree on how many clauses are registered (four:
    the offered rate, the generator CPU, the offline checks, the CPU-seconds per live ask) and name the mock rule's origin."""
    readme = (ROOT / "tools" / "loadtest" / "README.md").read_text(encoding="utf-8")
    assert model.MOCK_CPU_VOID_PCT == 50.0 and model.HARNESS_RULE_LABEL == "harness rule (not pre-registered)"
    for name, text in (("scripts/loadtest_report.py", report.__doc__), ("tools/loadtest/README.md", readme)):
        assert model.HARNESS_RULE_LABEL in text, name
        assert "five pre-registered" not in text and "those five" not in text, name
        assert "four pre-registered" in text, name
    constant = next(line for line in Path(model.__file__).read_text(encoding="utf-8").splitlines()
                    if line.startswith("MOCK_CPU_VOID_PCT"))
    assert model.HARNESS_RULE_LABEL in constant                       # the label sits where the number is defined


def test_void_when_an_offline_salt_check_failed(tmp_path):
    result, out = verdict(tmp_path, salt_ok=False)
    assert result == "VOID" and any("offline salt check failed" in r for r in out["verdict"]["void_reasons"])


def test_void_when_the_offline_checks_ran_on_another_pool(tmp_path):
    result, out = verdict(tmp_path, salt_pool="q" * 64)
    assert result == "VOID" and any("different pool" in r for r in out["verdict"]["void_reasons"])


def test_void_when_cpu_seconds_per_live_ask_are_below_80_percent_of_the_fly_embed_cost(tmp_path):
    # 1.2 CPU-s/s over 200 s / 540 asks = 0.444; the cost is 0.60 -> floor 0.48 -> the asks did not really embed
    result, out = verdict(tmp_path, fly_embed_s=0.60)
    assert result == "VOID" and any("did not embed" in r for r in out["verdict"]["void_reasons"])
    assert verdict(tmp_path / "ok", fly_embed_s=0.55)[0] == "PASS"             # 0.444 >= 0.8 x 0.55 = 0.44


def test_void_when_live_questions_were_sent_unsalted(tmp_path):
    result, out = verdict(tmp_path, unsalted_live=3)
    assert result == "VOID" and any("without a salt" in r for r in out["verdict"]["void_reasons"])


def test_void_when_salted_questions_collide(tmp_path):
    result, out = verdict(tmp_path, duplicate_salt=True)
    assert result == "VOID" and any("cache keys collided" in r for r in out["verdict"]["void_reasons"])
    assert out["validity"]["distinct_salted"] < out["validity"]["live_sends"]


@pytest.mark.parametrize("params,shape,vus", [
    ({"think_min_s": 1.0, "think_max_s": 3.0}, True, 1000), ({"mix": {"cached": 0.9, "live_pool": 0.1}}, True, 1000),
    ({"p_evidence": 0.5}, True, 1000), ({"upload_period_s": 60.0}, True, 1000), (None, False, 1000), (None, True, 20)])
def test_void_when_the_traffic_model_was_not_the_registered_one(tmp_path, params, shape, vus):
    result, out = verdict(tmp_path, params=params, shape=shape, vus=vus)
    assert result == "VOID" and any("not the pre-registered traffic model" in r for r in out["verdict"]["void_reasons"])


@pytest.mark.parametrize("missing,needle", [
    ({"salt_file": False}, "salt_check.json"), ({"generator_cpu_files": 0}, "generator CPU samples"),
    ({"mock_cpu_file": False}, "mock CPU samples"), ({"server_cpu_file": False}, "server CPU samples"),
    ({"fly_embed_s": None}, "fly_embed_s"), ({"ledger": False}, "ledger_rows")])
def test_a_missing_validity_input_is_a_void_never_a_pass(tmp_path, missing, needle):
    result, out = verdict(tmp_path, **missing)
    assert result == "VOID" and any("missing input" in r and needle in r for r in out["verdict"]["void_reasons"]), out["verdict"]


def test_the_desktop_embed_figure_is_never_a_fallback_for_the_fly_one(tmp_path):
    _, out = verdict(tmp_path, fly_embed_s=None)
    assert out["validity"]["fly_embed_s"] is None
    cli = report.compute_report(tmp_path / "run", fly_embed_s=0.4)               # the explicit Fly-measured value on the command line
    assert cli["verdict"]["result"] == "PASS" and cli["validity"]["fly_embed_s"] == 0.4


def test_the_local_graph_check_not_having_run_is_a_warning_not_a_void(tmp_path):
    result, out = verdict(tmp_path, local_graph_ran=False)
    assert result == "PASS" and any("local-graph" in w for w in out["verdict"]["warnings"])


# =====================================================================================================================
# FAIL: each gate alone
# =====================================================================================================================

def test_fail_when_ttfe_p95_exceeds_1_5_seconds(tmp_path):
    result, out = verdict(tmp_path, ttfe_slow_fraction=0.1, ttfe_slow=2.0)
    assert result == "FAIL" and out["gates"]["ttfe_p95"]["status"] == "fail" and out["metrics"]["live_ttfe_p95"] == 2.0
    assert any("ttfe_p95" in r for r in out["verdict"]["fail_reasons"])


def test_ttfe_p95_exactly_at_the_limit_passes(tmp_path):
    result, out = verdict(tmp_path, ttfe_slow_fraction=0.1, ttfe_slow=1.5)
    assert result == "PASS" and out["metrics"]["live_ttfe_p95"] == 1.5


def test_fail_on_any_dropped_ask(tmp_path):
    result, out = verdict(tmp_path, drops=1)
    assert result == "FAIL" and out["gates"]["drops"]["value"] == 1 and out["metrics"]["dropped"] == 1


def test_an_admitted_ask_that_never_reached_a_first_event_counts_as_infinitely_slow(tmp_path):
    result, out = verdict(tmp_path, drops=40)                                    # 7 % of the live asks: p95 is one of them
    assert result == "FAIL" and out["metrics"]["live_ttfe_unreached"] == 40
    assert out["metrics"]["live_ttfe_p95"] is None and out["gates"]["ttfe_p95"]["status"] == "fail"
    json.dumps(out)


def test_ttfe_percentiles_are_nearest_rank_and_inf_sorts_last():
    assert report.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10 and report.percentile([1, 2, 3, 4], 50) == 2
    assert report.percentile([], 95) is None and report.percentile([5], 99) == 5
    assert report.percentile([0.1] * 94 + [math.inf] * 6, 95) == math.inf and report.percentile([0.1] * 95 + [math.inf] * 5, 95) == 0.1


def test_errors_count_the_admission_429s_and_503s_with_no_carve_out(tmp_path):
    probe = RunBuilder(tmp_path / "probe")
    probe.write()
    limit = math.ceil(0.005 * probe.live_measured)                              # shed asks are live asks: 0.5 % of 540 = 3
    for status, label in ((429, "shed_429"), (503, "shed_503")):
        result, out = verdict(tmp_path / label, shed=limit, shed_status=status)
        assert result == "FAIL" and out["gates"]["errors"]["status"] == "fail"
        assert out["metrics"]["errors_by_status"] == {str(status): limit} and out["metrics"]["errors_by_outcome"] == {label: limit}
        assert "live_asks" in out["gates"]["errors"]["note"]
    below, out = verdict(tmp_path / "below", shed=limit - 1)
    assert below == "PASS" and out["metrics"]["error_rate_live_asks"] < 0.005 and out["metrics"]["errors"] == limit - 1


def test_shedding_two_percent_of_live_asks_fails_even_though_the_all_request_rate_looks_tiny(tmp_path):
    probe = RunBuilder(tmp_path / "probe")
    probe.write()
    shed = round(0.02 * probe.live_measured)
    result, out = verdict(tmp_path / "shed", shed=shed)                         # zero read errors
    assert out["metrics"]["error_rate"] < 0.005                                  # ~0.2 % of ALL requests: the lenient denominator passes it
    assert out["metrics"]["error_rate_live_asks"] == pytest.approx(0.02, abs=0.002) and out["metrics"]["error_rate_asks"] > 0.005
    assert result == "FAIL" and out["gates"]["errors"]["status"] == "fail" and out["gates"]["errors"]["value"] == pytest.approx(0.02, abs=0.002)


def test_a_failing_read_trips_the_all_request_denominator_alone(tmp_path):
    probe = RunBuilder(tmp_path / "probe")
    probe.write()
    limit = math.ceil(0.005 * probe.requests_measured)
    result, out = verdict(tmp_path / "reads", extra_errors=limit)
    assert result == "FAIL" and out["metrics"]["error_rate_asks"] == 0 and out["metrics"]["error_rate"] >= 0.005
    assert verdict(tmp_path / "reads_below", extra_errors=limit - 1)[0] == "PASS"


def test_errors_include_error_events_and_failed_reads(tmp_path):
    n = RunBuilder(tmp_path / "probe")
    n.write()
    limit = math.ceil(0.005 * n.requests_measured)
    result, out = verdict(tmp_path / "ev", error_events=limit)
    assert result == "FAIL" and out["metrics"]["errors_by_status"] == {"200:error_event": limit}
    result, out = verdict(tmp_path / "rd", extra_errors=limit)
    assert result == "FAIL" and out["metrics"]["errors_by_status"] == {"500": limit}


def test_fail_when_server_cpu_p95_exceeds_70_percent(tmp_path):
    result, out = verdict(tmp_path, server_cpu_hot_fraction=0.1, server_cpu_hot=85.0)
    assert result == "FAIL" and out["gates"]["server_cpu"]["status"] == "fail" and out["server"]["cpu_pct_p95"] == 85.0
    assert verdict(tmp_path / "edge", server_cpu=70.0)[0] == "PASS"


def test_a_few_hot_seconds_below_the_p95_do_not_fail_the_cpu_gate(tmp_path):
    assert verdict(tmp_path, server_cpu_hot_fraction=0.02, server_cpu_hot=95.0)[0] == "PASS"


@pytest.mark.parametrize("kw,needle", [
    ({"ledger_delta": {"done": -1}}, "ledger rows"), ({"ledger_delta": {"done": 1}}, "ledger rows"),
    ({"unsettled": 2}, "still reserved"), ({"ledger_delta": {"done": -1, "error": 1}}, "ledger done")])
def test_fail_when_ledger_rows_differ_from_the_generators_terminals(tmp_path, kw, needle):
    result, out = verdict(tmp_path, **kw)
    assert result == "FAIL" and out["gates"]["ledger_rows"]["status"] == "fail" and needle in out["gates"]["ledger_rows"]["note"]


def test_fail_on_a_cap_overshoot_and_pass_when_the_cap_held(tmp_path):
    probe = RunBuilder(tmp_path / "probe")
    probe.write()
    result, out = verdict(tmp_path / "over", cap=probe.admitted - 5)
    assert result == "FAIL" and out["gates"]["overshoot"]["value"] == 5
    assert verdict(tmp_path / "held", cap=probe.admitted)[0] == "PASS"
    assert verdict(tmp_path / "none")[1]["gates"]["overshoot"]["note"].startswith("not applicable")


def test_a_fail_reports_the_maximum_sustained_live_rate(tmp_path):
    result, out = verdict(tmp_path, server_cpu_hot_fraction=0.1, server_cpu_hot=90.0)
    assert result == "FAIL" and out["verdict"]["max_sustained_live_rate"] == pytest.approx(2.7, abs=0.05)
    text = report.render(out)
    assert "max sustained completed live rate 2.7" in text


def test_every_failing_gate_is_listed_not_just_the_first(tmp_path):
    result, out = verdict(tmp_path, drops=2, server_cpu_hot_fraction=0.1, server_cpu_hot=90.0, ttfe_slow_fraction=0.1)
    assert result == "FAIL" and len(out["verdict"]["fail_reasons"]) >= 3


# =====================================================================================================================
# VOID outranks FAIL; reasons are all listed
# =====================================================================================================================

def test_a_run_that_is_both_void_and_failing_is_void_and_still_shows_what_would_fail(tmp_path):
    result, out = verdict(tmp_path, gen_cpu=80.0, drops=3, server_cpu_hot_fraction=0.1, server_cpu_hot=90.0)
    assert result == "VOID"
    assert out["verdict"]["fail_reasons"] and out["verdict"]["also_failing_if_valid"] == out["verdict"]["fail_reasons"]
    assert "would also FAIL" in report.render(out)


def test_every_void_reason_is_listed(tmp_path):
    result, out = verdict(tmp_path, live_rate=2.0, gen_cpu=80.0, mock_cpu=60.0, salt_ok=False, fly_embed_s=5.0)
    reasons = out["verdict"]["void_reasons"]
    assert result == "VOID" and len(reasons) >= 5
    for needle in ("offered live rate", "generator", "mock", "offline salt check", "did not embed"):
        assert any(needle in r for r in reasons), needle


# =====================================================================================================================
# the smoke profile, the CLI and its exit codes
# =====================================================================================================================

def test_the_smoke_profile_scales_the_floor_and_skips_the_staging_only_inputs(tmp_path):
    kw = dict(live_rate=0.06, cached_rate=0.05, vus=20, shape=False, ledger=False, mock_cpu_file=False, server_cpu_file=False,
              fly_embed_s=None, generator_cpu_files=1, concurrent_per_worker=1)
    builder = RunBuilder(tmp_path / "run", **kw)
    builder.write()
    gate = report.compute_report(builder.root, profile="gate", vus=20)
    assert gate["verdict"]["result"] == "VOID"                                    # unscaled: 0.06/s is far below 2.47/s
    smoke = report.compute_report(builder.root, profile="smoke", vus=20)
    assert smoke["verdict"]["result"] == "PASS" and smoke["profile"] == "smoke"
    assert smoke["validity"]["skipped_checks"] and any("server CPU" in s for s in smoke["validity"]["skipped_checks"])
    low = RunBuilder(tmp_path / "low", **{**kw, "live_rate": 0.02})
    low.write()
    assert report.compute_report(low.root, profile="smoke", vus=20)["verdict"]["result"] == "VOID"


@pytest.mark.parametrize("kw,code,word", [({}, 0, "PASS"), ({"drops": 1}, 1, "FAIL"), ({"live_rate": 1.0}, 2, "VOID")])
def test_the_cli_writes_run_json_prints_the_verdict_and_returns_the_exit_code(tmp_path, capsys, kw, code, word):
    root = RunBuilder(tmp_path / "run", **kw).write()
    assert report.main([str(root)]) == code
    out = capsys.readouterr().out
    assert f"VERDICT: {word}" in out and "PENDING OWNER SIGN-OFF" in out and "NOT gated" in out
    written = json.loads((root / "run.json").read_text("utf-8"))
    assert written["verdict"]["result"] == word and written["run_id"] == "s2-fixture"


def test_the_cli_accepts_the_fly_embed_cost_and_a_phase_selection(tmp_path, capsys):
    root = RunBuilder(tmp_path / "run", fly_embed_s=None).write()
    assert report.main([str(root)]) == 2
    assert report.main([str(root), "--fly-embed-s", "0.4", "--out", str(tmp_path / "out.json")]) == 0
    assert json.loads((tmp_path / "out.json").read_text("utf-8"))["phases_measured"] == ["steady", "soak"]
    assert report.main([str(root), "--fly-embed-s", "0.4", "--phases", "steady"]) in (0, 2)


def test_a_missing_generator_log_exits_3_and_writes_no_run_json(tmp_path, capsys):
    root = tmp_path / "empty"
    (root / "cpu").mkdir(parents=True)
    assert report.main([str(root)]) == 3
    assert not (root / "run.json").exists() and "nothing to judge" in capsys.readouterr().err
    (root / "generator").mkdir()
    (root / "generator" / "events.w0.p1.jsonl").write_text("", "utf-8")
    assert report.main([str(root)]) == 3


def test_logs_from_two_runs_in_one_directory_are_refused(tmp_path, capsys):
    root = RunBuilder(tmp_path / "run").write()
    stale = root / "generator" / "events.w9.p9.jsonl"
    stale.write_text(json.dumps({"kind": "phase", "name": "steady", "ts": 1.0, "run_id": "another-run", "worker": 9}) + "\n", "utf-8")
    assert report.main([str(root)]) == 3 and "several runs" in capsys.readouterr().err


def test_a_torn_last_line_in_a_generator_log_is_survivable(tmp_path):
    root = RunBuilder(tmp_path / "run").write()
    log = sorted((root / "generator").glob("events.*.jsonl"))[0]
    log.write_text(log.read_text("utf-8") + '{"kind": "ask", "klass": "li', "utf-8")
    assert report.compute_report(root)["verdict"]["result"] == "PASS"


def test_clock_skew_between_generators_does_not_change_the_rates(tmp_path):
    a = report.compute_report(RunBuilder(tmp_path / "a", skew=0.0).write())
    b = report.compute_report(RunBuilder(tmp_path / "b", skew=3.0).write())
    assert a["metrics"]["offered_live_rate"] == pytest.approx(b["metrics"]["offered_live_rate"], abs=0.02)
    assert b["verdict"]["result"] == "PASS"


def test_the_report_module_imports_without_semigraph_locust_or_gevent():
    import subprocess

    code = ("import sys; sys.modules['semigraph']=None; sys.modules['locust']=None; sys.modules['gevent']=None; "
            f"sys.path.insert(0, {str(ROOT)!r}); import importlib.util as u; "
            f"s=u.spec_from_file_location('r', {str(ROOT / 'scripts' / 'loadtest_report.py')!r}); m=u.module_from_spec(s); s.loader.exec_module(m); print('ok')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "ok" in done.stdout, done.stderr


def test_cpu_outside_the_measured_phases_is_ignored_including_the_spike_between_steady_and_soak(tmp_path):
    # every machine is pinned at 95 % and the server burns 50 CPU-s/s (not 1.2) in the ramp, the spike and the fault phase
    result, out = verdict(tmp_path, spike_s=40, outside_cpu=95.0, outside_server_rate=50.0)
    assert result == "PASS", out["verdict"]
    assert out["validity"]["generator_cpu_max"] == {"gen-0": 40.0, "gen-1": 40.0} and out["validity"]["mock_cpu_max"] == 20.0
    assert out["server"]["cpu_pct_p95"] == 45.0
    assert out["validity"]["cpu_s_per_live_ask"] == pytest.approx(1.2 * 200 / 540, abs=0.02)
    assert [p["name"] for p in out["phases"]] == ["ramp", "steady", "spike", "soak", "fault"]
    assert out["metrics"]["window_s"] == 200.0
    # ... and the same samples DO count when the spike phase is measured
    root = tmp_path / "run"
    wide = report.compute_report(root, phases=("steady", "spike", "soak"))
    assert wide["verdict"]["result"] == "VOID" and any("generator" in r for r in wide["verdict"]["void_reasons"])


def test_void_when_a_salted_live_ask_was_answered_from_the_cache(tmp_path):
    out, builder = run_report(tmp_path, live_cache_hits=3)
    assert out["verdict"]["result"] == "VOID" and any("answered from the cache" in r for r in out["verdict"]["void_reasons"])
    assert out["validity"]["generator_live_admitted"] == builder.admitted             # a cache hit has no ledger row: not "admitted"


def test_void_when_a_generator_process_restarted_mid_run(tmp_path):
    result, out = verdict(tmp_path, restart_worker=True)
    assert result == "VOID" and any("restarted mid-run" in r and "0: 2 logs" in r for r in out["verdict"]["void_reasons"])


def test_iteration_overruns_are_counted_so_a_short_offered_rate_can_be_explained(tmp_path):
    _, out = verdict(tmp_path, iterations=50, overran=5)
    assert out["metrics"]["iterations"] == 50 and out["metrics"]["iterations_overran"] == 5


# =====================================================================================================================
# VOID reasons carry their source; the zero-overshoot sub-run profile
# =====================================================================================================================

def test_every_void_reason_is_tagged_with_where_its_clause_comes_from(tmp_path):
    result, out = verdict(tmp_path / "pre", live_rate=1.0)
    clauses = out["verdict"]["void_clauses"]
    assert result == "VOID" and {c["source"] for c in clauses} == {"pre-registered"}
    assert out["verdict"]["void_reasons"][0].startswith("[pre-registered] ")
    assert out["verdict"]["void_without_a_pre_registered_clause"] is False
    result, out = verdict(tmp_path / "missing", salt_file=False)
    assert [c["source"] for c in out["verdict"]["void_clauses"]] == ["input missing"]
    assert out["verdict"]["void_without_a_pre_registered_clause"] is True
    result, out = verdict(tmp_path / "harness", restart_worker=True, unsalted_live=2)
    assert {c["source"] for c in out["verdict"]["void_clauses"]} == {"harness-added"}
    assert out["verdict"]["void_without_a_pre_registered_clause"] is True
    assert "rests on no pre-registered clause" in report.render(out) and "[harness-added]" in report.render(out)
    mixed = verdict(tmp_path / "mixed", live_rate=1.0, restart_worker=True)[1]
    assert {c["source"] for c in mixed["verdict"]["void_clauses"]} == {"pre-registered", "harness-added"}
    assert mixed["verdict"]["void_without_a_pre_registered_clause"] is False


def subrun_report(tmp_path, **kw):
    kw = {"shape": False, "other_phases": False, **kw}
    builder = RunBuilder(tmp_path / "run", **kw)
    builder.write()
    return report.compute_report(builder.root, profile="subrun"), builder


def test_the_subrun_profile_judges_rows_and_overshoot_and_reports_the_refusals_it_expects(tmp_path):
    probe = RunBuilder(tmp_path / "probe", shape=False, other_phases=False, shed=60)
    probe.write()
    out, builder = subrun_report(tmp_path, shed=60, cap=probe.admitted)
    assert out["profile"] == "subrun" and out["verdict"]["result"] == "PASS", out["verdict"]
    assert out["gates"]["overshoot"]["status"] == "pass" and out["gates"]["overshoot"]["value"] == 0
    assert out["gates"]["errors"]["status"] == "reported" and out["gates"]["errors"]["would_be"] == "fail"   # the cap refuses on purpose
    assert out["gates"]["ledger_rows"]["status"] == "pass"
    gate_profile = report.compute_report(builder.root, profile="gate")
    assert gate_profile["verdict"]["result"] == "VOID" and any("no staged shape" in r for r in gate_profile["verdict"]["void_reasons"])


def test_the_subrun_fails_on_an_overshoot_and_is_void_without_a_cap(tmp_path):
    probe = RunBuilder(tmp_path / "probe", shape=False, other_phases=False)
    probe.write()
    over, _ = subrun_report(tmp_path / "over", cap=probe.admitted - 4)
    assert over["verdict"]["result"] == "FAIL" and over["gates"]["overshoot"]["value"] == 4
    no_cap, _ = subrun_report(tmp_path / "nocap")
    assert no_cap["verdict"]["result"] == "VOID" and any("has no cap" in r or "overshoot" in r for r in no_cap["verdict"]["void_reasons"])
    bad_rows, _ = subrun_report(tmp_path / "rows", cap=probe.admitted, ledger_delta={"done": -2})
    assert bad_rows["verdict"]["result"] == "FAIL" and bad_rows["gates"]["ledger_rows"]["status"] == "fail"


def test_the_subrun_still_needs_the_registered_load_and_valid_checks(tmp_path):
    probe = RunBuilder(tmp_path / "probe", shape=False, other_phases=False)
    probe.write()
    low, _ = subrun_report(tmp_path / "low", cap=probe.admitted, live_rate=1.0)
    assert low["verdict"]["result"] == "VOID" and any("offered live rate" in r for r in low["verdict"]["void_reasons"])
    other_model, _ = subrun_report(tmp_path / "model", cap=probe.admitted, params={"think_min_s": 1.0})
    assert other_model["verdict"]["result"] == "VOID"
    assert any("not the pre-registered traffic model" in r for r in other_model["verdict"]["void_reasons"])


def test_the_cli_accepts_the_subrun_profile(tmp_path, capsys):
    root = RunBuilder(tmp_path / "run", shape=False, other_phases=False).write()
    assert report.main([str(root), "--profile", "subrun"]) == 2                   # no cap in the ledger: VOID, not a silent PASS
    assert "profile=subrun" in capsys.readouterr().out

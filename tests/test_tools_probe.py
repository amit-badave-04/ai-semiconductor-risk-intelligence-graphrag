"""tools/probe: the fresh-machine throttle probe (M5a I5, docs/v2/M5_DECISIONS.md section 3, "Fresh-machine throttle probe").

On a newly created shared-cpu-2x machine: one full-core loop for an hour, iterations per second recorded every second beside the
steal time of /proc/stat, then an idle stretch with a short pulse every minute to see how fast the burst balance refills.
``burn.py`` produces the raw JSON lines; ``report.py`` turns them into ``probe.json``. Everything here runs on a fake clock and
fake work: nothing waits, nothing needs Linux (``/proc/stat`` is fed as text), and the one real run is two seconds long.

What is pinned: the /proc/stat parser and the steal arithmetic; one sample per second with the right iterations per second when
the work slows down; the raw file is flushed second by second (a crash keeps what was measured); the analysis finds the second
the throughput fell for good, ignores a single dip, reports the sustained share, the steal before and after, and the refill
by the pulses; the report refuses to claim a throttle from too little data; the modules are stdlib only (the tools image).
"""

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.probe import burn, report  # noqa: E402

PROBE_DIR = ROOT / "tools" / "probe"

STAT_A = """cpu  1000 0 500 8000 100 0 50 20 0 0
cpu0 600 0 250 4000 50 0 25 15 0 0
cpu1 400 0 250 4000 50 0 25 5 0 0
intr 12345 1 2 3
ctxt 99
"""
STAT_B = """cpu  1180 0 560 8160 100 0 60 80 0 0
cpu0 780 0 280 4040 50 0 30 75 0 0
cpu1 400 0 280 4120 50 0 30 5 0 0
intr 12999 1 2 3
"""


class Clock:
    """A clock the fake work advances; exact binary fractions only, so second boundaries are exact."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def slowing_work(clock, *, fast=0.25, slow=1.0, at=3.0):
    """One chunk takes ``fast`` seconds until the clock reaches ``at``, ``slow`` after it."""
    def work():
        clock.now += fast if clock.now < at else slow
    return work


def burn_samples(duration_s=8, **kwargs):
    clock = Clock()
    work = kwargs.pop("work", None) or slowing_work(clock)
    rows = []
    stats = kwargs.pop("read_stat", lambda: None)
    burn.run_burn(duration_s, chunk=250, work=work, clock=clock, wall=lambda: 1_000.0 + clock.now,
                  cpu_time=lambda: clock.now, read_stat=stats, sink=rows.append, **kwargs)
    return rows


# --- /proc/stat ------------------------------------------------------------------------------------------------------------

def test_the_proc_stat_text_is_parsed_into_totals_and_steal_per_cpu():
    snap = burn.parse_proc_stat(STAT_A)
    assert snap.total["steal"] == 20 and snap.total["user"] == 1000
    assert [c["steal"] for c in snap.cpus] == [15, 5]
    assert snap.total_jiffies == 1000 + 0 + 500 + 8000 + 100 + 0 + 50 + 20                    # guest columns are inside user


@pytest.mark.parametrize("text", ["", "garbage\n", "cpu  a b c\n", "cpu0 1 2 3 4 5 6 7 8\n"])
def test_a_missing_or_malformed_proc_stat_is_none_not_an_exception(text):
    assert burn.parse_proc_stat(text) is None


def test_old_kernels_with_fewer_columns_have_no_steal_but_still_parse():
    snap = burn.parse_proc_stat("cpu  10 0 5 80 1 0 0\ncpu0 10 0 5 80 1 0 0\n")
    assert snap is not None and snap.total["steal"] == 0


def test_steal_fraction_is_the_steal_jiffies_over_all_jiffies_between_two_snapshots():
    a, b = burn.parse_proc_stat(STAT_A), burn.parse_proc_stat(STAT_B)
    frac, by_cpu = burn.steal_fraction(a, b)
    delta_total = b.total_jiffies - a.total_jiffies
    assert frac == pytest.approx((80 - 20) / delta_total)
    assert by_cpu[0] == pytest.approx((75 - 15) / (b.cpus[0]["total"] - a.cpus[0]["total"]))
    assert by_cpu[1] == pytest.approx(0.0)


def test_steal_fraction_is_none_when_nothing_elapsed_or_a_snapshot_is_missing():
    a = burn.parse_proc_stat(STAT_A)
    assert burn.steal_fraction(a, a) == (None, None)
    assert burn.steal_fraction(None, a) == (None, None) and burn.steal_fraction(a, None) == (None, None)


# --- the burn loop -----------------------------------------------------------------------------------------------------------

def test_one_sample_per_second_with_the_iterations_per_second_of_that_second():
    rows = burn_samples(8)
    assert [r["t"] for r in rows] == list(range(1, 9))
    assert all(r["phase"] == "burn" for r in rows)
    assert [r["iter_per_s"] for r in rows[:3]] == pytest.approx([1000.0] * 3)               # 4 chunks of 250 per second
    assert [r["iter_per_s"] for r in rows[4:]] == pytest.approx([250.0] * 4)                # 1 chunk of 250 per second
    assert all(r["iterations"] % 250 == 0 for r in rows)
    assert rows[0]["elapsed_s"] == pytest.approx(1.0) and rows[0]["wall"] == pytest.approx(1001.0)


def test_cpu_share_is_cpu_seconds_over_wall_seconds_and_steal_comes_from_the_two_snapshots():
    clock = Clock()
    texts = iter([STAT_A, STAT_B] + [STAT_B] * 20)
    rows = []
    burn.run_burn(2, chunk=250, work=slowing_work(clock, at=99), clock=clock, wall=lambda: 5.0,
                  cpu_time=lambda: clock.now * 0.5, read_stat=lambda: burn.parse_proc_stat(next(texts)), sink=rows.append)
    assert rows[0]["cpu_share"] == pytest.approx(0.5)
    assert rows[0]["steal_frac"] == pytest.approx((80 - 20) / (burn.parse_proc_stat(STAT_B).total_jiffies
                                                                - burn.parse_proc_stat(STAT_A).total_jiffies))
    assert rows[0]["steal_by_cpu"] == pytest.approx(burn.steal_fraction(burn.parse_proc_stat(STAT_A),
                                                                         burn.parse_proc_stat(STAT_B))[1])
    assert rows[1]["steal_frac"] is None                                                  # no jiffies passed between two equal snapshots


def test_a_machine_without_proc_stat_still_measures_throughput():
    rows = burn_samples(3)
    assert rows and all(r["steal_frac"] is None and r["steal_by_cpu"] is None for r in rows)


def test_the_sink_gets_every_sample_as_it_is_measured_so_a_crash_keeps_the_earlier_ones():
    clock = Clock()
    seen = []

    def work():
        if clock.now >= 3.0:
            raise RuntimeError("the machine went away")
        clock.now += 0.25

    with pytest.raises(RuntimeError):
        burn.run_burn(10, chunk=250, work=work, clock=clock, wall=lambda: 0.0, cpu_time=lambda: 0.0,
                      read_stat=lambda: None, sink=seen.append)
    assert [r["t"] for r in seen] == [1, 2, 3]


def test_burn_refuses_a_non_positive_duration_or_chunk():
    for kwargs in ({"duration_s": 0}, {"duration_s": -1}, {"duration_s": 5, "chunk": 0}):
        with pytest.raises(ValueError):
            burn.run_burn(**{"sink": lambda r: None, **kwargs})


def test_pulses_after_the_burn_measure_a_short_burst_each_period():
    clock = Clock()
    rows = []
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    burn.run_pulses(180, every_s=60, pulse_s=2, chunk=250, work=slowing_work(clock, at=0.0, slow=0.25), clock=clock,
                    wall=lambda: 7.0, cpu_time=lambda: 0.0, read_stat=lambda: None, sleep=sleep, sink=rows.append)
    assert [r["phase"] for r in rows] == ["pulse"] * 3
    assert [r["t"] for r in rows] == [60, 120, 180]                    # seconds after the idle stretch began
    assert all(r["iter_per_s"] == pytest.approx(1000.0) and r["elapsed_s"] == pytest.approx(2.0) for r in rows)
    assert slept == [58.0, 58.0, 58.0]                                  # it slept between pulses, not through them


def test_the_real_loop_runs_for_a_moment_and_writes_the_raw_file_and_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-test")
    code = burn.main(["--burn-s", "2", "--out", str(tmp_path)])
    assert code == 0
    lines = [json.loads(line) for line in (tmp_path / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["t"] for line in lines] == [1, 2] and all(line["iter_per_s"] > 0 for line in lines)
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["machine_id"] == "machine-test" and meta["burn_s"] == 2 and meta["cpu_count"] >= 1
    assert "FLY_API_TOKEN" not in json.dumps(meta)                           # the environment is not copied


# --- the analysis -------------------------------------------------------------------------------------------------------------

def series(values, *, phase="burn", start=1, steal=None):
    return [{"t": start + i, "phase": phase, "iter_per_s": float(v), "steal_frac": None if steal is None else steal(i)}
            for i, v in enumerate(values)]


def test_the_analysis_finds_the_second_the_throughput_fell_for_good_and_the_sustained_share():
    values = [1000] * 40 + [250] * 60
    result = report.analyze(series(values, steal=lambda i: 0.0 if i < 40 else 0.5))
    assert result["burn_seconds"] == 100 and result["baseline_iter_s"] == pytest.approx(1000.0)
    assert result["throttled"] is True and result["onset_s"] == 41
    assert result["sustained_iter_s"] == pytest.approx(250.0) and result["sustained_fraction"] == pytest.approx(0.25)
    assert result["steal"]["available"] is True
    assert result["steal"]["mean_before_onset"] == pytest.approx(0.0) and result["steal"]["mean_after_onset"] == pytest.approx(0.5)


def test_a_single_dip_is_not_a_throttle_and_a_flat_run_reports_none():
    flat = [1000] * 30 + [400] + [1000] * 30
    result = report.analyze(series(flat))
    assert result["throttled"] is False and result["onset_s"] is None
    assert result["sustained_fraction"] == pytest.approx(1.0)


def test_the_baseline_ignores_noise_and_the_threshold_is_a_fraction_of_it():
    values = [1000, 990, 1010, 1005, 995, 1000, 1002, 998, 1001, 999] + [920] * 20           # 8 % under: above the 90 % line
    assert report.analyze(series(values))["throttled"] is False
    values = [1000] * 10 + [880] * 20                                                         # 12 % under: below it
    assert report.analyze(series(values))["onset_s"] == 11


def test_too_little_data_is_reported_as_insufficient_not_as_a_verdict():
    result = report.analyze(series([1000] * 5))
    assert result["throttled"] is None and result["onset_s"] is None
    assert "insufficient" in result["summary"].lower()
    assert report.analyze([])["throttled"] is None


def test_the_pulses_say_how_fast_the_balance_refilled():
    burn_part = series([1000] * 20 + [250] * 40)
    pulses = [{"t": 60, "phase": "pulse", "iter_per_s": 250.0}, {"t": 120, "phase": "pulse", "iter_per_s": 600.0},
              {"t": 180, "phase": "pulse", "iter_per_s": 980.0}, {"t": 240, "phase": "pulse", "iter_per_s": 1000.0}]
    result = report.analyze(burn_part + pulses)
    assert [p["fraction_of_baseline"] for p in result["pulses"]] == pytest.approx([0.25, 0.6, 0.98, 1.0])
    assert result["refill_to_baseline_s"] == 180 and result["pulses"][0]["t_after_burn_s"] == 60


def test_no_refill_is_none_when_no_pulse_gets_back_to_ninety_percent():
    result = report.analyze(series([1000] * 20 + [250] * 40) + [{"t": 60, "phase": "pulse", "iter_per_s": 300.0}])
    assert result["refill_to_baseline_s"] is None


def test_the_summary_states_the_observation_in_plain_words():
    text = report.analyze(series([1000] * 40 + [250] * 60))["summary"]
    assert "41" in text and "25" in text                                 # the second and the share


def test_report_main_reads_a_directory_and_writes_probe_json(tmp_path):
    (tmp_path / "samples.jsonl").write_text("\n".join(json.dumps(r) for r in series([1000] * 30 + [300] * 30)) + "\n", encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({"machine_id": "m1", "burn_s": 60}), encoding="utf-8")
    out = tmp_path / "probe.json"
    assert report.main([str(tmp_path), "--out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["meta"]["machine_id"] == "m1" and doc["analysis"]["throttled"] is True
    assert len(doc["per_second"]) == 60 and set(doc["per_second"][0]) == {"t", "iter_per_s", "steal_frac"}


def test_report_main_fails_clearly_on_an_empty_or_missing_directory(tmp_path, capsys):
    assert report.main([str(tmp_path)]) == 2
    assert "samples.jsonl" in capsys.readouterr().err


def test_a_torn_last_line_from_a_killed_run_is_skipped_not_fatal(tmp_path):
    good = "\n".join(json.dumps(r) for r in series([1000] * 20))
    (tmp_path / "samples.jsonl").write_text(good + '\n{"t": 21, "phase": "bu', encoding="utf-8")
    assert len(report.load_samples(tmp_path / "samples.jsonl")) == 20


# --- packaging -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("module", ["__init__.py", "burn.py", "report.py"])
def test_the_probe_modules_are_stdlib_only(module):
    tree = ast.parse((PROBE_DIR / module).read_text(encoding="utf-8"))
    imported = {(node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
                for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in getattr(node, "names", [None])}
    allowed = set(sys.stdlib_module_names) | {"tools"}
    assert imported <= allowed, imported - allowed

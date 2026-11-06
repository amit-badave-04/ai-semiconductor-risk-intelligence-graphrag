"""tools/probe/w1d_burn.py: the burn step of window W1d (docs/specs/upload-embed-timeout.md, Plan 2 "Burn step"). Nothing here touches Fly.

The burn runs ON the api machine, because the quota is the machine's. ``tools/probe/burn.py`` is shipped inside a ``python -c`` command over
``flyctl ssh console -C`` (the way ``memsample.py`` is: no file is uploaded, no stdin is relied on), detached, with a stop file; the driver
polls its samples, applies ``report.py``'s onset rule locally and stops it. The remote machine is a fake with a virtual clock, so a 600 s
burn runs in milliseconds. What is pinned:

* the command: the burn's text as one quoted program and one payload word, detached, with the 600 s cap and the stop file on the machine
  (the cap is the end of the burn when the driver dies); the text really runs under ``exec`` in ``python -c``;
* the stop decision as a pure function: at least 300 samples AND ``report.analyze`` says throttled (below 90 % of the baseline for 5 s in
  a row); an onset at second 115 still waits for second 300; an onset at 450 stops soon after it; no onset runs to the end of the burn;
* the step against the fake machine: stop by criterion, by the end of the burn or by a deadline; a burn that never starts; flaky reads; a
  burn that ignores the stop (never followed by an upload); every sample recorded; the burn never started after it was stopped.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.probe import burn, report, w1b, w1d_burn  # noqa: E402

APP = "semigraph-stg"


# --- the command ---------------------------------------------------------------------------------------------------------------------

def test_the_burn_is_shipped_as_one_quoted_program_and_one_payload_word_detached_with_a_cap_and_a_stop_file():
    import base64
    import zlib
    command = w1d_burn.burn_command(w1d_burn.burn_source(), burn_s=600)
    words = shlex.split(command)
    assert words[:2] == ["python", "-c"] and words[2] == w1b.BOOTSTRAP and "'" not in w1b.BOOTSTRAP
    assert zlib.decompress(base64.b64decode(words[3])).decode() == w1d_burn.burn_source() == Path(burn.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert words[4:] == ["--daemon", "--out", w1d_burn.BURN_DIR, "--burn-s", "600", "--stop-file", w1d_burn.BURN_STOP]
    assert len(command) < 12_000 and not any(ch.isspace() for ch in words[3])


def test_the_burn_text_runs_as_text_under_exec_inside_python_dash_c_and_stops_at_a_stop_file(tmp_path):
    words = shlex.split(w1d_burn.burn_command(w1d_burn.burn_source(), burn_s=3))
    words = [w for w in words if w != "--daemon"]                              # os.fork does not exist everywhere; the detach is tested in test_tools_probe
    words[words.index("--out") + 1] = str(tmp_path / "out")
    words[words.index("--stop-file") + 1] = str(tmp_path / "burn.stop")
    done = subprocess.run([sys.executable, "-c", words[2], words[3], *words[4:]], capture_output=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr.decode()
    rows = [json.loads(line) for line in (tmp_path / "out" / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["t"] for row in rows] == [1, 2, 3] and all(row["phase"] == "burn" for row in rows)
    (tmp_path / "burn.stop").write_text("", encoding="utf-8")
    done = subprocess.run([sys.executable, "-c", words[2], words[3], *words[4:]], capture_output=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr.decode()
    assert len((tmp_path / "out" / "samples.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_the_defaults_are_the_specs_600_s_cap_and_300_s_floor():
    assert (w1d_burn.DEFAULT_BURN_S, w1d_burn.DEFAULT_MIN_BURN_S) == (600, 300)
    assert w1d_burn.BURN_STOP.startswith("/tmp/") and w1d_burn.BURN_DIR.startswith("/tmp/")


# --- the samples as they are read ------------------------------------------------------------------------------------------------------------

def row(t, rate=1000.0, **extra):
    return {"t": t, "phase": "burn", "iter_per_s": rate, "steal_frac": None, "steal_by_cpu": None, "elapsed_s": 1.0, **extra}


def test_new_samples_are_asked_for_by_line_number_so_a_banner_or_a_line_ending_cannot_drift_the_offset():
    tail = w1d_burn.SampleTail(w1d_burn.BURN_SAMPLES)
    assert tail.command() == f"tail -n +1 {w1d_burn.BURN_SAMPLES}"
    text = "Connecting to fdaa:0:1::3... complete\r\n" + "".join(json.dumps(row(t)) + "\r\n" for t in (1, 2, 3))
    assert tail.feed(text) == 3 and tail.command() == f"tail -n +4 {w1d_burn.BURN_SAMPLES}"
    assert [r["t"] for r in tail.samples] == [1, 2, 3]


def test_a_torn_last_line_is_not_counted_and_is_read_whole_next_time():
    tail = w1d_burn.SampleTail(w1d_burn.BURN_SAMPLES)
    assert tail.feed(json.dumps(row(1)) + "\n" + json.dumps(row(2))[:20]) == 1
    assert tail.command().startswith("tail -n +2 ")
    assert tail.feed(json.dumps(row(2)) + "\n" + json.dumps(row(3)) + "\n") == 2
    assert [r["t"] for r in tail.samples] == [1, 2, 3]


def test_lines_that_are_not_samples_are_ignored():
    tail = w1d_burn.SampleTail(w1d_burn.BURN_SAMPLES)
    assert tail.feed('{"header": 1}\n[1, 2]\n"x"\n\n{"no_t": 1}\n') == 0 and tail.samples == []


# --- the stop decision: a pure function of the samples ----------------------------------------------------------------------------------------

def series(seconds, onset=None, fast=1000.0, slow=250.0):
    return [row(t, fast if onset is None or t < onset else slow) for t in range(1, seconds + 1)]


def test_it_never_stops_before_300_samples_even_when_the_throttle_showed_at_second_115():
    assert report.analyze(series(299, onset=115))["throttled"] is True                  # W0's machine throttled from second 115
    assert w1d_burn.should_stop(series(299, onset=115)) is False
    assert w1d_burn.should_stop(series(300, onset=115)) is True


def test_it_stops_once_the_throttle_shows_after_300_and_not_before_five_slow_seconds():
    assert w1d_burn.should_stop(series(453, onset=450)) is False                         # four slow seconds (450 to 453) are not an onset (5 in a row)
    assert w1d_burn.should_stop(series(454, onset=450)) is True
    assert w1d_burn.should_stop(series(305, onset=299)) is True


def test_a_machine_that_never_throttles_is_not_stopped_by_the_criterion():
    assert w1d_burn.should_stop(series(600)) is False and w1d_burn.should_stop(series(300)) is False


def test_a_single_dip_is_not_a_throttle_and_too_little_data_is_not_a_decision():
    dip = series(400)
    dip[350]["iter_per_s"] = 100.0
    assert w1d_burn.should_stop(dip) is False and w1d_burn.should_stop([]) is False and w1d_burn.should_stop(series(10, onset=3)) is False


def test_the_minimum_is_a_parameter_and_idle_pulses_do_not_count_as_burn_seconds():
    assert w1d_burn.should_stop(series(120, onset=40), min_s=100) is True
    pulses = [{"t": 400 + i, "phase": "pulse", "iter_per_s": 1000.0} for i in range(5)]
    assert w1d_burn.burn_seconds(series(100) + pulses) == 100


# --- the step against a fake machine --------------------------------------------------------------------------------------------------------------

class FakeBurnHost:
    """The api machine as the burn step sees it, with a virtual clock: every flyctl call costs ``call_s`` seconds of it, the burn writes one
    sample per second of it and ends by itself after ``burn_s`` seconds or one second after the stop file appears."""

    def __init__(self, *, onset=115, burn_s=600, call_s=2.0, start_rc=0, ignore_stop=False, never_writes=False, fail_reads=(), fast=1000.0, slow=250.0):
        self.__dict__.update(locals())
        self.clock, self.started_at, self.stopped_at, self.calls, self.reads = 0.0, None, None, [], 0

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def rate(self, t):
        return self.fast if self.onset is None or t < self.onset else self.slow

    def available(self):
        if self.started_at is None or self.never_writes:
            return 0
        elapsed = int(self.clock - self.started_at)
        if self.stopped_at is not None and not self.ignore_stop:
            elapsed = min(elapsed, int(self.stopped_at - self.started_at) + 1)
        return min(elapsed, self.burn_s)

    def __call__(self, args, timeout):
        self.clock += self.call_s
        command = args[args.index("-C") + 1]
        self.calls.append((round(self.clock, 1), command))
        if command.startswith("python -c"):
            if self.start_rc == 0:
                self.started_at = self.clock
            return w1b.Result(self.start_rc, b"", b"")
        if command.startswith("touch"):
            self.stopped_at = self.clock if self.stopped_at is None else self.stopped_at
            return w1b.Result(0, b"", b"")
        if command.startswith("tail -n +"):
            self.reads += 1
            if self.reads in self.fail_reads:
                return w1b.Result(255, b"", b"ssh: connection reset")
            if self.never_writes or self.started_at is None:
                return w1b.Result(1, b"", b"tail: cannot open: No such file or directory")
            first = int(command.split()[2].lstrip("+"))
            lines = "".join(json.dumps(row(t, self.rate(t), wall=1_000_000.0 + t)) + "\n" for t in range(first, self.available() + 1))
            return w1b.Result(0, lines.encode(), b"")
        raise AssertionError(f"unexpected command {command!r}")


def step(host, **kwargs):
    fly = w1b.Fly(APP, host)
    return w1d_burn.run_burn_step(fly, source="# burn", now=host.now, sleep=host.sleep, log=lambda line: None, **kwargs)


def test_the_throttle_at_115_s_is_acted_on_at_the_first_poll_after_300_samples_and_the_burn_is_stopped_and_confirmed():
    host = FakeBurnHost(onset=115)
    result = step(host)
    assert result["started"] is True and result["stopped_by"] == "criterion" and result["criterion_met"] is True
    assert 300 <= result["samples"] <= 345 and result["confirmed_stopped"] is True and result["read_failures"] == 0
    assert result["throttled"] is True and result["onset_s"] == 115 and result["sustained_fraction"] == pytest.approx(0.25)
    commands = [c for _, c in host.calls]
    assert commands[0].startswith("python -c") and sum(c.startswith("python -c") for c in commands) == 1
    assert commands.count(f"touch {w1d_burn.BURN_STOP}") == 1 and commands.index(f"touch {w1d_burn.BURN_STOP}") > 0
    after_touch = commands[commands.index(f"touch {w1d_burn.BURN_STOP}") + 1:]
    assert len(after_touch) == 2 and all(c.startswith("tail -n +") for c in after_touch)       # one read for the rest, one to see it stopped
    assert host.available() == result["samples"]                                                # no sample was left unread


def test_the_time_of_the_stop_request_and_the_last_sample_of_the_machines_own_clock_are_recorded():
    host = FakeBurnHost(onset=115)
    result = step(host)
    assert result["stop_requested_at"] < result["ended_at"] and result["ended_at"] - result["stop_requested_at"] >= 2 * w1d_burn.SETTLE_S
    touch_at = next(at for at, command in host.calls if command.startswith("touch"))
    assert result["stop_requested_at"] == pytest.approx(touch_at - host.call_s)                 # taken just before the touch command goes out
    assert result["last_sample_wall"] == 1_000_000.0 + result["samples"]                        # the fake stamps each sample with 1,000,000 + t


def test_every_sample_is_recorded_per_second_without_the_per_cpu_lists():
    result = step(FakeBurnHost(onset=115))
    per_second = result["per_second"]
    assert [p["t"] for p in per_second] == list(range(1, result["samples"] + 1))
    assert set(per_second[0]) == {"t", "iter_per_s", "steal_frac"} and per_second[0]["iter_per_s"] == 1000.0 and per_second[-1]["iter_per_s"] == 250.0


def test_a_late_onset_stops_soon_after_it_and_never_before_the_floor():
    result = step(FakeBurnHost(onset=450))
    assert result["stopped_by"] == "criterion" and 455 <= result["samples"] <= 500 and result["onset_s"] == 450


def test_no_onset_runs_to_the_end_of_the_burn_and_the_upload_may_still_follow_the_rule_decides():
    host = FakeBurnHost(onset=None)
    result = step(host)
    assert result["stopped_by"] == "burn_ended" and result["criterion_met"] is False and result["samples"] == 600
    assert result["throttled"] is False and result["confirmed_stopped"] is True and result["elapsed_s"] >= 600


def test_a_burn_that_never_writes_is_given_up_after_the_start_wait_and_cannot_be_confirmed_stopped():
    host = FakeBurnHost(never_writes=True)
    result = step(host)
    assert result["stopped_by"] == "not_started" and result["samples"] == 0 and result["confirmed_stopped"] is False
    assert result["elapsed_s"] < 120 and f"touch {w1d_burn.BURN_STOP}" in [c for _, c in host.calls]


def test_a_start_command_that_fails_is_reported_and_the_stop_file_is_still_made():
    host = FakeBurnHost(start_rc=1)
    result = step(host)
    assert result["started"] is False and result["stopped_by"] == "not_started" and f"touch {w1d_burn.BURN_STOP}" in [c for _, c in host.calls]


def test_a_read_that_fails_is_counted_and_the_decision_waits_for_the_next_one():
    host = FakeBurnHost(onset=115, fail_reads=(3, 4, 5))
    result = step(host)
    assert result["read_failures"] == 3 and result["stopped_by"] == "criterion" and result["confirmed_stopped"] is True


def test_reads_that_fail_at_the_start_are_not_a_burn_that_did_not_start_and_the_burn_runs_its_floor():
    host = FakeBurnHost(onset=115, fail_reads=(1, 2))                                  # a cold ssh start: the first two reads fail, the burn runs fine
    result = step(host)
    assert result["read_failures"] == 2 and result["stopped_by"] == "criterion" and result["burn_seconds"] >= 300
    assert result["confirmed_stopped"] is True and result["criterion_met"] is True
    assert sum(c.startswith("python -c") for _, c in host.calls) == 1 and sum(c.startswith("touch") for _, c in host.calls) == 1


def test_not_started_needs_an_answer_from_the_machine_that_there_is_nothing_not_a_failed_read():
    answered = w1d_burn.SampleTail(w1d_burn.BURN_SAMPLES)
    assert answered.answered is False and answered.samples == []
    nothing_yet = FakeBurnHost(never_writes=True)                                      # tail answers "No such file": the machine says there is no burn
    assert step(nothing_yet)["stopped_by"] == "not_started"
    transport = FakeBurnHost(never_writes=True, fail_reads=set(range(1, 1000)))        # every read fails at the transport: nothing is known
    result = step(transport)
    assert result["stopped_by"] == "deadline" and result["samples"] == 0 and result["confirmed_stopped"] is False


def test_a_burn_that_stops_writing_at_second_100_is_reported_as_the_short_burn_it_was():
    host = FakeBurnHost(onset=115, burn_s=100)                                         # killed (OOM) at 100 s: reads keep working and find nothing new
    result = step(host)
    assert result["stopped_by"] == "deadline" and result["burn_seconds"] == 100 and result["samples"] == 100
    assert result["confirmed_stopped"] is True and result["criterion_met"] is False    # confirmed stopped says nothing about how long it ran: the caller checks burn_seconds


def test_a_burn_that_ignores_the_stop_is_not_confirmed_stopped_so_no_upload_may_follow():
    host = FakeBurnHost(onset=115, ignore_stop=True)
    result = step(host)
    assert result["stopped_by"] == "criterion" and result["confirmed_stopped"] is False


def test_reads_that_never_work_end_at_the_deadline_with_no_decision():
    host = FakeBurnHost(onset=115, fail_reads=set(range(1, 1000)))
    result = step(host)
    assert result["stopped_by"] == "deadline" and result["confirmed_stopped"] is False and result["criterion_met"] is False
    assert result["read_failures"] >= 3 and result["elapsed_s"] <= 600 + w1d_burn.BURN_SLACK_S + 60


def test_the_burn_is_started_once_and_never_after_it_was_stopped():
    host = FakeBurnHost(onset=115)
    step(host)
    commands = [c for _, c in host.calls]
    assert [i for i, c in enumerate(commands) if c.startswith("python -c")] == [0]


def test_only_the_registered_app_may_be_named():
    fly = w1b.Fly(APP, FakeBurnHost())
    with pytest.raises(w1b.W1bError):
        fly(["ssh", "console", "-a", "semigraph", "-C", "touch /tmp/x"])
    assert os.path.basename(w1d_burn.__file__) == "w1d_burn.py"

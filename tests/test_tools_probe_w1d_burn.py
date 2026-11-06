"""tools/probe/w1d_burn.py: the burn step of window W1d (docs/specs/upload-embed-timeout.md, Plan 2 "Burn step"). Nothing here touches Fly.

The burn runs ON the api machine, because the quota is the machine's. ``tools/probe/burn.py`` is shipped inside a ``python -c`` command over
``flyctl ssh console -C`` (the way ``memsample.py`` is: no file is uploaded, no stdin is relied on), detached, into a directory and with a stop file
that belong to THIS run only. The live canary of 2026-11-06 showed that a full-core burn uses up the machine's pooled quota and starves sshd as well:
the driver's reads worked until sample 223 and failed from then on, and the stop file could not be created. So the burn is NEVER steered over ssh
while it runs: the driver starts it, waits locally for the whole burn plus a margin (no ssh traffic at all), then reads the whole samples file and
confirms the burn is over. The remote machine is a fake with a virtual clock (and a clock of its own, offset from the driver's), so a 600 s burn runs in
milliseconds. What is pinned:

* the command: the burn's text as one quoted program and one payload word, detached, with the 600 s cap, a per-run output directory and a per-run stop file
  (a file left by an earlier burn on the same machine, the canary's, can never pass for this run's); the text really runs under ``exec`` in ``python -c``;
* the step against the fake machine: no ssh call at all between the start and burn_s + margin; then one read of the whole file and a second read that finds
  nothing new confirm a complete burn; a short file (the burn died); a burn whose seconds DRIFT (the live canary's 600 samples took more than 600 s, so the
  margin can end before the last sample): the step waits locally for the rest, with no ssh, and only then falls back to the stop file; a file that is still
  growing after that (stop-file fallback, working or not); reads that fail, retried for minutes (ssh needs time to recover after a full-core burn) and then
  given up; a burn that never wrote (a stale file is not read); a start that failed after the detach; the evidence for the canary re-run (every read attempt,
  the burn's span on the machine's clock);
* the onset analysis (``report.py``) is recorded on the full samples and decides nothing;
* the clocks: the machine's last sample is stamped by the machine, the driver's confirmation by the driver, and the step never subtracts one from the other.
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
LEGACY_SAMPLES, LEGACY_STOP = "/tmp/w1d-burn/samples.jsonl", "/tmp/w1d-burn.stop"       # the fixed paths of the first design (the canary of 2026-11-06 left a file there)
BASE_EPOCH = 1_000_000.0


# --- the command ---------------------------------------------------------------------------------------------------------------------

def test_the_burn_is_shipped_as_one_quoted_program_and_one_payload_word_detached_with_a_cap_and_a_per_run_stop_file():
    import base64
    import zlib
    paths = w1d_burn.BurnPaths("run1")
    command = w1d_burn.burn_command(w1d_burn.burn_source(), burn_s=600, paths=paths)
    words = shlex.split(command)
    assert words[:2] == ["python", "-c"] and words[2] == w1b.BOOTSTRAP and "'" not in w1b.BOOTSTRAP
    assert zlib.decompress(base64.b64decode(words[3])).decode() == w1d_burn.burn_source() == Path(burn.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert words[4:] == ["--daemon", "--out", paths.dir, "--burn-s", "600", "--stop-file", paths.stop]
    assert len(command) < 12_000 and not any(ch.isspace() for ch in words[3])


def test_the_burn_text_runs_as_text_under_exec_inside_python_dash_c_and_stops_at_a_stop_file(tmp_path):
    words = shlex.split(w1d_burn.burn_command(w1d_burn.burn_source(), burn_s=3, paths=w1d_burn.BurnPaths("run1")))
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


def test_the_defaults_are_the_specs_600_s_cap_and_300_s_floor_and_the_driver_waits_a_margin_past_the_cap():
    assert (w1d_burn.DEFAULT_BURN_S, w1d_burn.DEFAULT_MIN_BURN_S) == (600, 300)
    assert w1d_burn.MARGIN_S == 30.0 and w1d_burn.READ_TIMEOUT_S <= 60                    # a short timeout per try ...
    assert w1d_burn.READ_BUDGET_S >= 480                                                   # ... repeated for minutes: ssh needed minutes to recover after the canary's burn
    assert w1d_burn.MAX_EXTRA_WAITS == 2 and w1d_burn.EXTRA_WAIT_FACTOR >= 1.2             # a drifting burn is waited for locally, a bounded number of times
    assert w1d_burn.BURN_SLACK_S >= w1d_burn.MARGIN_S + 2 * w1d_burn.READ_BUDGET_S         # the sampler outlives the burn phase, slow reads included


def test_every_run_has_its_own_directory_and_stop_file_and_never_the_fixed_ones_of_the_first_design():
    first, second = w1d_burn.new_run_id(), w1d_burn.new_run_id()
    assert first != second and first.isalnum() and second.isalnum()
    one, two = w1d_burn.BurnPaths(first), w1d_burn.BurnPaths(second)
    assert one.dir.startswith("/tmp/") and one.stop.startswith("/tmp/") and one.samples == f"{one.dir}/samples.jsonl"
    assert len({one.dir, one.stop, two.dir, two.stop}) == 4
    assert LEGACY_SAMPLES not in (one.samples, two.samples) and LEGACY_STOP not in (one.stop, two.stop)
    with pytest.raises(ValueError):
        w1d_burn.BurnPaths("../etc")                                                   # the id is part of a path that goes into a shell command


# --- the samples as they are read ------------------------------------------------------------------------------------------------------------

def row(t, rate=1000.0, **extra):
    return {"t": t, "phase": "burn", "iter_per_s": rate, "steal_frac": None, "steal_by_cpu": None, "elapsed_s": 1.0, **extra}


def test_new_samples_are_asked_for_by_line_number_so_a_banner_or_a_line_ending_cannot_drift_the_offset():
    path = w1d_burn.BurnPaths("run1").samples
    tail = w1d_burn.SampleTail(path)
    assert tail.command() == f"tail -n +1 {path}"
    text = "Connecting to fdaa:0:1::3... complete\r\n" + "".join(json.dumps(row(t)) + "\r\n" for t in (1, 2, 3))
    assert tail.feed(text) == 3 and tail.command() == f"tail -n +4 {path}"
    assert [r["t"] for r in tail.samples] == [1, 2, 3]


def test_a_torn_last_line_is_not_counted_and_is_read_whole_next_time():
    tail = w1d_burn.SampleTail(w1d_burn.BurnPaths("run1").samples)
    assert tail.feed(json.dumps(row(1)) + "\n" + json.dumps(row(2))[:20]) == 1
    assert tail.command().startswith("tail -n +2 ")
    assert tail.feed(json.dumps(row(2)) + "\n" + json.dumps(row(3)) + "\n") == 2
    assert [r["t"] for r in tail.samples] == [1, 2, 3]


def test_lines_that_are_not_samples_are_ignored():
    tail = w1d_burn.SampleTail(w1d_burn.BurnPaths("run1").samples)
    assert tail.feed('{"header": 1}\n[1, 2]\n"x"\n\n{"no_t": 1}\n') == 0 and tail.samples == []


# --- the onset analysis: a pure function of the samples, recorded and never used to stop the burn --------------------------------------------------

def series(seconds, onset=None, fast=1000.0, slow=250.0):
    return [row(t, fast if onset is None or t < onset else slow) for t in range(1, seconds + 1)]


def test_the_onset_counts_only_after_300_samples_even_when_the_throttle_showed_at_second_115():
    assert report.analyze(series(299, onset=115))["throttled"] is True                  # W0's machine throttled from second 115
    assert w1d_burn.onset_after_floor(series(299, onset=115)) is False
    assert w1d_burn.onset_after_floor(series(300, onset=115)) is True


def test_an_onset_needs_five_slow_seconds_in_a_row_and_a_late_one_still_counts():
    assert w1d_burn.onset_after_floor(series(453, onset=450)) is False                    # four slow seconds (450 to 453) are not an onset (5 in a row)
    assert w1d_burn.onset_after_floor(series(454, onset=450)) is True
    assert w1d_burn.onset_after_floor(series(305, onset=299)) is True


def test_a_machine_that_never_throttles_has_no_onset():
    assert w1d_burn.onset_after_floor(series(600)) is False and w1d_burn.onset_after_floor(series(300)) is False


def test_a_single_dip_is_not_a_throttle_and_too_little_data_is_not_an_onset():
    dip = series(400)
    dip[350]["iter_per_s"] = 100.0
    assert w1d_burn.onset_after_floor(dip) is False and w1d_burn.onset_after_floor([]) is False and w1d_burn.onset_after_floor(series(10, onset=3)) is False


def test_the_minimum_is_a_parameter_and_idle_pulses_do_not_count_as_burn_seconds():
    assert w1d_burn.onset_after_floor(series(120, onset=40), min_s=100) is True
    pulses = [{"t": 400 + i, "phase": "pulse", "iter_per_s": 1000.0} for i in range(5)]
    assert w1d_burn.burn_seconds(series(100) + pulses) == 100
    assert not hasattr(w1d_burn, "should_stop")                                          # nothing decides to stop on the onset any more


# --- the step against a fake machine --------------------------------------------------------------------------------------------------------------

class FakeBurnHost:
    """The api machine as the burn step sees it, with a virtual clock for the driver and a clock of its own for the machine (``clock_offset`` ahead):
    every flyctl call costs ``call_s`` seconds, the burn writes one sample a second, from ``late_start_s`` after the start command returned, for
    ``burn_s`` samples (None: the cap in its command line) and ends one second after the stop file appears, unless ``ignore_stop``. ``bucket_s`` is how
    long one sample takes (1.0: none drift; burn.py's seconds drift under throttling). ``ssh_down_until``: every ssh call issued before this driver time
    fails (sshd starved, then slow to recover). The files are
    those of the command line: a read of any other path says "No such file", except the fixed path of the first design, which holds a complete,
    stale file when ``stale`` is set. ``start_rc`` != 0 with ``runs_anyway`` is an ssh that failed after the burn was detached."""

    def __init__(self, *, onset=115, burn_s=None, call_s=2.0, start_rc=0, runs_anyway=False, ignore_stop=False, never_writes=False, fail_reads=(),
                 fast=1000.0, slow=250.0, late_start_s=0.0, clock_offset=37.0, stale=False, bucket_s=1.0, ssh_down_until=None):
        self.__dict__.update(locals())
        del self.__dict__["self"]
        self.clock, self.started_at, self.stopped_at, self.calls, self.reads = 0.0, None, None, [], 0
        self.samples_path = self.stop_path = self.cap = None

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def machine_time(self, local=None):
        return BASE_EPOCH + self.clock_offset + (self.clock if local is None else local)

    def rate(self, t):
        return self.fast if self.onset is None or t < self.onset else self.slow

    def writing_since(self):
        return self.started_at + self.late_start_s

    def available(self):
        if self.started_at is None or self.never_writes:
            return 0
        written = max(0, int((self.clock - self.writing_since()) / self.bucket_s))
        if self.stopped_at is not None and not self.ignore_stop:
            written = min(written, max(0, int((self.stopped_at - self.writing_since()) / self.bucket_s)) + 1)
        return min(written, self.burn_s if self.burn_s is not None else self.cap)

    def sample_lines(self, first, last):
        return "".join(json.dumps(row(t, self.rate(t), wall=self.machine_time(self.writing_since() + t * self.bucket_s))) + "\n" for t in range(first, last + 1))

    def __call__(self, args, timeout):
        issued = self.clock
        self.clock += self.call_s
        command = args[args.index("-C") + 1]
        self.calls.append({"issued": issued, "returned": self.clock, "command": command, "timeout": timeout})
        if command.startswith("python -c"):
            words = shlex.split(command)
            self.samples_path = words[words.index("--out") + 1] + "/samples.jsonl"
            self.stop_path = words[words.index("--stop-file") + 1]
            self.cap = int(words[words.index("--burn-s") + 1])
            if self.start_rc == 0 or self.runs_anyway:
                self.started_at = self.clock
            return w1b.Result(self.start_rc, b"", b"" if self.start_rc == 0 else b"ssh: connection closed")
        if self.ssh_down_until is not None and issued < self.ssh_down_until:
            return w1b.Result(255, b"", b"ssh: handshake timed out")
        if command.startswith("touch"):
            if command == f"touch {self.stop_path}":
                self.stopped_at = self.clock if self.stopped_at is None else self.stopped_at
            return w1b.Result(0, b"", b"")
        if command.startswith("tail -n +"):
            self.reads += 1
            _, _, offset, path = command.split()
            if self.reads in self.fail_reads or (-1 in self.fail_reads and self.reads > 1):
                return w1b.Result(255, b"", b"ssh: connection reset")
            first = int(offset.lstrip("+"))
            if path == LEGACY_SAMPLES and self.stale:
                return w1b.Result(0, "".join(json.dumps(row(t, 1000.0, wall=BASE_EPOCH - 3600 + t)) + "\n" for t in range(first, 601)).encode(), b"")
            if path != self.samples_path or self.started_at is None or self.never_writes:
                return w1b.Result(1, b"", b"tail: cannot open: No such file or directory")
            return w1b.Result(0, self.sample_lines(first, self.available()).encode(), b"")
        raise AssertionError(f"unexpected command {command!r}")

    def after_start(self):
        """The calls after the start command, in order."""
        return self.calls[1:]

    def reads_made(self):
        return [c for c in self.calls if c["command"].startswith("tail -n +")]

    def touches(self):
        return [c for c in self.calls if c["command"].startswith("touch")]


def step(host, **kwargs):
    fly = w1b.Fly(APP, host)
    kwargs.setdefault("run_id", "run1")
    return w1d_burn.run_burn_step(fly, source="# burn", now=host.now, sleep=host.sleep, log=lambda line: None, **kwargs)


def test_nothing_is_sent_over_ssh_between_the_start_and_the_end_of_the_burn_plus_its_margin():
    host = FakeBurnHost(onset=115)
    result = step(host)
    start = host.calls[0]
    assert start["command"].startswith("python -c") and sum(c["command"].startswith("python -c") for c in host.calls) == 1
    quiet_until = start["returned"] + 600 + w1d_burn.MARGIN_S
    assert host.after_start() and all(c["issued"] >= quiet_until for c in host.after_start())      # not one call while the burn can still be running
    assert result["waited_s"] >= 600 + w1d_burn.MARGIN_S and result["elapsed_s"] >= 630


def test_the_margin_is_a_parameter_and_the_wait_is_start_return_plus_burn_plus_margin():
    host = FakeBurnHost(onset=115)
    step(host, margin_s=10.0)
    assert all(c["issued"] >= host.calls[0]["returned"] + 610.0 for c in host.after_start())
    assert host.after_start()[0]["issued"] < host.calls[0]["returned"] + 610.0 + w1d_burn.WAIT_CHUNK_S      # and not a whole chunk late


def test_a_complete_600_line_file_is_confirmed_by_one_whole_file_read_and_a_second_read_that_finds_nothing_new():
    host = FakeBurnHost(onset=115)
    result = step(host)
    paths = w1d_burn.BurnPaths("run1")
    assert result["started"] is True and result["stopped_by"] == "burn_ended" and result["confirmed_stopped"] is True and result["burn_complete"] is True
    assert result["samples"] == 600 and result["burn_seconds"] == 600 and result["read_failures"] == 0
    assert [c["command"] for c in host.after_start()] == [f"tail -n +1 {paths.samples}", f"tail -n +601 {paths.samples}"]      # the whole file once, then nothing new
    assert host.touches() == [] and host.reads_made()[1]["issued"] - host.reads_made()[0]["returned"] >= w1d_burn.CONFIRM_GAP_S
    assert result["run_id"] == "run1" and result["samples_path"] == paths.samples


def test_the_onset_analysis_runs_on_the_full_samples_afterwards_and_is_recorded_not_used():
    result = step(FakeBurnHost(onset=115))
    assert result["throttled"] is True and result["onset_s"] == 115 and result["sustained_fraction"] == pytest.approx(0.25)
    assert result["onset_after_floor"] is True and "criterion_met" not in result
    flat = step(FakeBurnHost(onset=None))                                              # no onset at all: the burn is just as complete, the upload may follow
    assert flat["throttled"] is False and flat["onset_after_floor"] is False
    assert flat["stopped_by"] == "burn_ended" and flat["confirmed_stopped"] is True and flat["burn_complete"] is True and flat["samples"] == 600


def test_every_sample_is_recorded_per_second_without_the_per_cpu_lists():
    result = step(FakeBurnHost(onset=115))
    per_second = result["per_second"]
    assert [p["t"] for p in per_second] == list(range(1, 601))
    assert set(per_second[0]) == {"t", "iter_per_s", "steal_frac"} and per_second[0]["iter_per_s"] == 1000.0 and per_second[-1]["iter_per_s"] == 250.0


def test_each_time_is_labelled_with_its_clock_the_machines_last_sample_and_the_drivers_confirmation_are_never_mixed():
    host = FakeBurnHost(onset=115, clock_offset=37.0)
    result = step(host)
    # the machine stamped its last sample with ITS clock: it is 37 s ahead of the driver's
    assert result["last_sample_wall_machine"] == pytest.approx(host.machine_time(host.writing_since() + 600))
    assert "last_sample_wall" not in result
    # the driver took the confirmation on its own clock: right after the second read returned
    assert result["confirmed_at"] == pytest.approx(host.reads_made()[1]["returned"])
    skewed = step(FakeBurnHost(onset=115, clock_offset=-500.0))
    assert skewed["confirmed_at"] == pytest.approx(result["confirmed_at"]) and skewed["last_sample_wall_machine"] == pytest.approx(result["last_sample_wall_machine"] - 537.0)


# --- a burn that is not complete -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("length", [450, 301, 100])
def test_a_short_file_is_a_burn_that_died_it_is_over_but_not_complete_and_no_stop_file_is_made(length):
    host = FakeBurnHost(onset=115, burn_s=length)                                      # killed (OOM) after `length` s; 450 and 301 are ABOVE the 300 s floor
    result = step(host)
    assert result["stopped_by"] == "burn_died" and result["samples"] == length and result["burn_seconds"] == length
    assert result["confirmed_stopped"] is True and result["burn_complete"] is False     # confirmed stopped says nothing about how long it ran
    assert host.touches() == [] and len(host.reads_made()) == 2


def test_a_file_that_is_still_growing_after_the_margin_gets_the_stop_file_as_a_fallback_and_is_confirmed_when_the_burn_obeys():
    host = FakeBurnHost(onset=115, burn_s=1500)                                        # a burn that does not end at its cap
    result = step(host)
    paths = w1d_burn.BurnPaths("run1")
    assert result["stopped_by"] == "stop_file" and result["confirmed_stopped"] is True and result["burn_complete"] is True and result["burn_seconds"] >= 599
    commands = [c["command"] for c in host.after_start()]
    assert commands[0] == f"tail -n +1 {paths.samples}" and commands[1].startswith("tail -n +6") and commands[2] == f"touch {paths.stop}"
    assert all(c.startswith("tail -n +") for c in commands[3:]) and len(commands) == 5      # whole file, a read that sees it grow, the touch, one read for the rest, one that finds nothing
    assert host.touches()[0]["issued"] >= host.calls[0]["returned"] + 600 + w1d_burn.MARGIN_S


def test_a_burn_that_ignores_the_stop_file_is_not_confirmed_stopped_so_no_upload_may_follow():
    host = FakeBurnHost(onset=115, burn_s=1500, ignore_stop=True)
    result = step(host)
    assert result["stopped_by"] == "stop_file" and result["confirmed_stopped"] is False
    assert len(host.touches()) == 1


def test_a_burn_whose_seconds_drift_by_five_percent_is_complete_at_the_first_read_and_needs_no_extra_wait():
    host = FakeBurnHost(onset=115, bucket_s=1.05)                                      # 600 samples take 630 s
    result = step(host)
    assert result["stopped_by"] == "burn_ended" and result["confirmed_stopped"] is True and result["burn_complete"] is True and result["samples"] == 600
    assert len(host.reads_made()) == 2 and host.touches() == []


def test_a_burn_whose_seconds_drift_by_ten_percent_is_still_writing_at_the_margin_so_the_step_waits_locally_for_the_rest_with_no_ssh_and_no_stop_file():
    host = FakeBurnHost(onset=115, bucket_s=1.10)                                      # 600 samples take 660 s: the margin ends at sample 574
    result = step(host)
    assert result["stopped_by"] == "burn_ended" and result["confirmed_stopped"] is True and result["burn_complete"] is True and result["samples"] == 600
    assert host.touches() == []
    reads = host.reads_made()
    assert len(reads) == 4                                                              # whole file, a read that sees it grow, the catch-up after the local wait, the read that finds nothing
    quiet = [c for c in host.calls if reads[1]["returned"] <= c["issued"] < reads[2]["issued"]]
    assert quiet == [] and reads[2]["issued"] - reads[1]["returned"] >= w1d_burn.MARGIN_S    # no ssh call in the extra wait; it lasts at least the margin


def test_a_burn_that_started_late_and_ends_by_itself_within_the_extra_waits_is_complete():
    host = FakeBurnHost(onset=115, late_start_s=300.0)                                 # it started writing 300 s after the start returned: still running at the first read
    result = step(host)
    assert result["stopped_by"] == "burn_ended" and result["confirmed_stopped"] is True and result["burn_complete"] is True and result["samples"] == 600
    assert host.touches() == []


def test_a_burn_that_is_far_behind_is_waited_for_twice_and_then_stopped_by_the_stop_file_and_is_short():
    host = FakeBurnHost(onset=115, bucket_s=2.0)                                       # 600 samples would take 1,200 s: still at sample 583 after two extra waits
    result = step(host)
    assert result["stopped_by"] == "stop_file" and result["confirmed_stopped"] is True and result["burn_complete"] is False
    assert 500 < result["burn_seconds"] < 599 and len(host.touches()) == 1
    touch_at = host.touches()[0]["issued"]
    quiet_end = host.calls[0]["returned"] + 600 + w1d_burn.MARGIN_S
    assert touch_at > quiet_end + 2 * w1d_burn.MARGIN_S                                 # after both extra waits, never inside the first quiet period


# --- the evidence the canary re-run measures --------------------------------------------------------------------------------------------------------

def test_every_read_attempt_is_recorded_with_its_offset_from_the_end_of_the_wait_and_the_burns_span_on_the_machines_clock():
    host = FakeBurnHost(onset=115, bucket_s=1.1, fail_reads=(1,))
    result = step(host)
    reads = result["reads"]
    assert [r["ok"] for r in reads] == [False, True, True, True, True] and result["read_failures"] == 1
    assert reads[0]["at_s"] == pytest.approx(0.0, abs=0.5) and [r["at_s"] for r in reads] == sorted(r["at_s"] for r in reads)
    assert 570 < reads[1]["new"] < 600 and reads[-1]["new"] == 0 and set(reads[0]) == {"at_s", "ok", "new", "no_file"}      # the first read that works sees most of the file
    assert result["burn_span_machine_s"] == pytest.approx(599 * 1.1)                     # last wall minus first wall, the machine's clock only: the drift is visible
    quick = step(FakeBurnHost(onset=115, never_writes=True))
    assert quick["burn_span_machine_s"] is None and quick["reads"][0]["no_file"] is True


# --- reads that fail after the burn ------------------------------------------------------------------------------------------------------------------

def test_a_read_that_fails_after_the_burn_is_retried_and_the_burn_is_still_confirmed():
    host = FakeBurnHost(onset=115, fail_reads=(1, 2, 3))                               # three failed tries of the whole-file read, the fourth works
    result = step(host)
    assert result["read_failures"] == 3 and result["stopped_by"] == "burn_ended" and result["confirmed_stopped"] is True and result["burn_complete"] is True
    reads = host.reads_made()
    assert len(reads) == 5 and all(b["issued"] - a["returned"] >= w1d_burn.READ_RETRY_S for a, b in zip(reads[:3], reads[1:4], strict=True))
    assert all(r["timeout"] <= w1d_burn.READ_TIMEOUT_S for r in reads)                  # the machine is idle again: a short timeout per try


def test_reads_that_keep_failing_are_retried_for_the_whole_budget_and_then_end_with_no_decision_no_stop_file_and_no_confirmation():
    host = FakeBurnHost(onset=115, fail_reads=set(range(1, 1000)))
    result = step(host)
    assert result["stopped_by"] == "read_failed" and result["confirmed_stopped"] is False and result["burn_complete"] is False
    assert result["samples"] == 0 and result["read_failures"] == len(host.reads_made()) >= 10                  # not a handful of tries: minutes of them
    assert host.touches() == []                                                         # the burn ended at its own cap long ago; a failing ssh cannot be asked for a stop file either
    wait_end = host.calls[0]["returned"] + 600 + w1d_burn.MARGIN_S
    assert wait_end + w1d_burn.READ_BUDGET_S - 30 <= host.reads_made()[-1]["issued"] <= wait_end + w1d_burn.READ_BUDGET_S
    assert result["elapsed_s"] <= 600 + w1d_burn.MARGIN_S + w1d_burn.READ_BUDGET_S + w1d_burn.READ_TIMEOUT_S + 10


def test_reads_that_fail_for_minutes_after_the_burn_ends_still_confirm_once_ssh_recovers():
    host = FakeBurnHost(onset=115, ssh_down_until=2.0 + 600 + 300)                     # the start works; ssh is down until 5 minutes after the burn's last write
    result = step(host)
    assert result["stopped_by"] == "burn_ended" and result["confirmed_stopped"] is True and result["burn_complete"] is True and result["samples"] == 600
    assert result["read_failures"] >= 4 and host.touches() == []
    failed = [r for r in result["reads"] if not r["ok"]]
    assert failed and failed[0]["at_s"] < 5 and failed[-1]["at_s"] > 200               # the offsets from the end of the wait show how long ssh took to recover


def test_the_confirming_read_that_keeps_failing_is_no_confirmation_even_though_the_whole_file_was_read():
    host = FakeBurnHost(onset=115, fail_reads={-1})                                    # the first read works, every later one fails
    result = step(host)
    assert result["samples"] == 600 and result["burn_seconds"] == 600 and result["stopped_by"] == "read_failed"
    assert result["confirmed_stopped"] is False and host.touches() == []


# --- a burn that never wrote -------------------------------------------------------------------------------------------------------------------------

def test_a_burn_that_never_wrote_is_not_started_the_machine_said_there_is_no_file_and_it_is_not_asked_twice():
    host = FakeBurnHost(never_writes=True)
    result = step(host)
    assert result["stopped_by"] == "not_started" and result["samples"] == 0 and result["confirmed_stopped"] is False and result["burn_complete"] is False
    assert len(host.reads_made()) == 1 and host.touches() == []


def test_a_complete_stale_file_of_an_earlier_burn_is_never_read_and_never_passes_for_this_run():
    host = FakeBurnHost(never_writes=True, stale=True)                                 # the canary left 600 lines at the old fixed path; this start wrote nothing
    result = step(host)
    assert result["stopped_by"] == "not_started" and result["samples"] == 0 and result["confirmed_stopped"] is False and result["burn_complete"] is False
    assert not any(LEGACY_SAMPLES in c["command"] or LEGACY_STOP in c["command"] for c in host.calls)
    assert all(w1d_burn.BurnPaths("run1").samples in c["command"] for c in host.reads_made())


def test_a_start_command_that_failed_is_reported_and_the_burn_is_judged_by_its_file_after_the_wait():
    gone = FakeBurnHost(start_rc=1)                                                    # the ssh failed before anything was launched
    result = step(gone)
    assert result["started"] is False and result["stopped_by"] == "not_started" and result["confirmed_stopped"] is False
    assert result["waited_s"] >= 630                                                    # it is not known to have failed before the detach: the wait is the same
    runs = FakeBurnHost(start_rc=1, runs_anyway=True)                                  # the ssh failed AFTER the burn was detached: the burn ran its 600 s
    result = step(runs)
    assert result["started"] is False and result["stopped_by"] == "burn_ended" and result["confirmed_stopped"] is True and result["burn_complete"] is True


# --- the rest -------------------------------------------------------------------------------------------------------------------------------------------

def test_two_steps_use_two_run_ids_unless_one_is_given_and_the_id_is_in_every_path_of_the_run():
    one, two = FakeBurnHost(), FakeBurnHost()
    fly_one, fly_two = w1b.Fly(APP, one), w1b.Fly(APP, two)
    first = w1d_burn.run_burn_step(fly_one, source="# burn", now=one.now, sleep=one.sleep, log=lambda line: None)
    second = w1d_burn.run_burn_step(fly_two, source="# burn", now=two.now, sleep=two.sleep, log=lambda line: None)
    assert first["run_id"] != second["run_id"] and first["samples_path"] != second["samples_path"]
    assert first["run_id"] in one.samples_path and first["run_id"] in one.stop_path


def test_the_burn_is_started_once_and_never_after_it_was_stopped():
    host = FakeBurnHost(onset=115, burn_s=1500)
    step(host)
    commands = [c["command"] for c in host.calls]
    assert [i for i, c in enumerate(commands) if c.startswith("python -c")] == [0]


def test_only_the_registered_app_may_be_named():
    fly = w1b.Fly(APP, FakeBurnHost())
    with pytest.raises(w1b.W1bError):
        fly(["ssh", "console", "-a", "semigraph", "-C", "touch /tmp/x"])
    assert os.path.basename(w1d_burn.__file__) == "w1d_burn.py"


@pytest.mark.parametrize("length, stopped_by, complete", [(600, "burn_ended", True), (599, "burn_died", False)])
def test_complete_means_the_burn_wrote_its_last_second_and_one_second_short_is_a_burn_that_died(length, stopped_by, complete):
    """burn.py always writes t == burn_s when it reaches its cap, so a file ending at 599 is a burn that died (or one silent for the confirmation gap
    before its last write): never a complete burn (the Opus verifier's tolerance-0 mutant survived before this test)."""
    result = step(FakeBurnHost(onset=115, burn_s=length))
    assert (result["stopped_by"], result["burn_complete"], result["burn_seconds"]) == (stopped_by, complete, length)


def test_the_confirmation_has_one_overall_deadline_so_the_memory_sampler_always_outlives_the_burn_step():
    """Reads that each fail for most of their own budget and then work once, against a burn that never stops: without one deadline for the whole
    confirmation the step ran on for thousands of seconds, past BURN_SLACK_S, the window the memory sampler is sized for."""
    host = FakeBurnHost(onset=115, burn_s=100000, ignore_stop=True, fail_reads=set(range(1, 100000)) - {45, 90, 135, 180})
    result = step(host)
    wait_end = host.calls[0]["returned"] + 600 + w1d_burn.MARGIN_S
    assert result["confirmed_stopped"] is False
    assert w1d_burn.CONFIRM_DEADLINE_S < w1d_burn.BURN_SLACK_S
    assert host.reads_made()[-1]["issued"] <= wait_end + w1d_burn.CONFIRM_DEADLINE_S
    assert result["elapsed_s"] <= 600 + w1d_burn.MARGIN_S + w1d_burn.CONFIRM_DEADLINE_S + w1d_burn.READ_TIMEOUT_S + 10

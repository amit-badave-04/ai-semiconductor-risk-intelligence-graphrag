"""tools/probe/w1d.py: the driver of window W1d, the upload-ceiling confirmation (docs/specs/upload-embed-timeout.md, Plan 2).
Nothing here touches Fly, a provider or a database.

The service is an ``httpx.MockTransport``, flyctl is a stub runner, ``flyctl logs`` is a fake ``Popen``, the clock is VIRTUAL (a 50 minute boot runs
in a fraction of a second) and ``subprocess.run`` / ``Popen`` are poisoned, so nothing can escape. The fake machine has a clock of its own (37 s ahead of the
driver's by default) that stamps its samples and the ``Date`` header of the upload's 202. What is pinned:

* the order, from one shared event log: the log stream starts first, then the sampler, then the burn, then (after the burn, with no flyctl call in between)
  the burn is confirmed over, and only then is the workspace created and the PDF uploaded; no ask is ever sent; the burn is never steered while it runs;
* a burn that cannot be confirmed over, never started, or did not run its full 600 s (it died, or is still running past the margin and does not obey the
  stop file) ends the boot before any upload (INCONCLUSIVE): the burn never runs beside the upload, and reads that fail after the burn are retried first;
* the idle gap before the upload is recorded once on the driver's clock (from the confirmation) and once on the machine's (last sample to the ``Date`` header
  of the acceptance), whatever the offset between the two, and is null rather than mixed when the header is missing;
* the stage line: read from the stream (a line that arrives late is waited for), else from a ``--no-tail`` snapshot taken as soon as the job ends (before the
  stream and the sampler are stopped), else the boot is INCONCLUSIVE; the stream is scrubbed before it is written, started with ``--machine`` and without it
  when the flag is rejected, and stopped exactly once, an error included;
* the runbook: a canary on the warm-up machine (a 2-page upload, ``--pdf``, ``canary.json``) comes before the measured boot and cannot reach the aggregate;
* the verdicts through the whole driver: PASS, INVALID (not throttled), FAIL (the ceiling stopped a valid job), INCONCLUSIVE (the harness deadline);
* the guards: the registered burn numbers cannot be changed from the command line, the harness deadline follows the job's budgets, the sampler outlives the
  burn, no secret reaches any output, file or flyctl argument, the target is the registered app, and ``aggregate`` judges W1d files only.
"""

import io
import json
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.probe import w1b, w1d, w1d_burn, w1d_judge  # noqa: E402

ORIGIN = "SENTINEL-origin-" + "o" * 40       # gitleaks:allow
ADMIN = "SENTINEL-admin-" + "a" * 40         # gitleaks:allow
WS_ID, WS_TOKEN = "w" * 32, "SENTINEL-wstoken-" + "t" * 30     # gitleaks:allow
APP, MACHINE, JOB_ID = "semigraph-stg", "e286f0aa00ee08", "jobid0123456789"
GUEST = {"cpu_kind": "shared", "cpus": 2, "memory_mb": 4096}
EPOCH = 1_800_000_000.0
STAGE_PREFIX = "2026-11-06T10:12:03Z app[e286f0aa00ee08] sin [info]2026-11-06 10:12:03,123 INFO semigraph.uploads.jobs "


@pytest.fixture(autouse=True)
def nothing_escapes_the_stubs(monkeypatch):
    def poisoned(*args, **kwargs):
        raise AssertionError("a real process was started")

    monkeypatch.setattr(subprocess, "run", poisoned)
    monkeypatch.setattr(subprocess, "Popen", poisoned)


def stage_text(embed_s=1300.0, tokens=16000, **changes):
    fields = {"parse_s": 12.25, "chunk_s": 1.5, "embed_s": embed_s, "compare_s": 0.0, "index_s": 5.125, "chunks": 60, "fresh": 60, "done": 60, "tokens": tokens}
    fields.update(changes)
    shown = " ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in fields.items())
    return STAGE_PREFIX + f"upload stages ws_hash=ab12cd34ef56 job_id={JOB_ID} {shown}"


class FakePopen:
    """``flyctl logs`` as a stream: lines pushed by the world arrive on ``stdout`` until ``terminate``."""

    def __init__(self, args, world, exits_at_once=None):
        self.args, self.world, self.queue = list(args), world, queue.Queue()
        self.terminated, self.killed, self.returncode = 0, 0, exits_at_once
        self.stdout = iter(self.queue.get, None)
        if exits_at_once is not None:
            self.queue.put(None)

    def push(self, text):
        self.queue.put((text if text.endswith("\n") else text + "\n").encode("utf-8"))

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if self.returncode is None:
            self.returncode = -15
            self.queue.put(None)
        self.world.events.append(("logs", "stop"))

    def kill(self):
        self.killed += 1
        self.queue.put(None)

    def wait(self, timeout=None):
        return self.returncode


class World:
    """One virtual clock and one event log shared by the service, flyctl, the log stream and the burn on the machine."""

    def __init__(self, *, onset=115, burn_s=None, job_s=1330.0, job_final="ready", error_code="timeout", stage=None, line_lag_s=0.0, in_stream=True,
                 in_snapshot=False, burn_ignores_stop=False, burn_never_writes=False, hang_job=False, logs_flag_rejected=False, machines=1,
                 sampler_ok=True, hwm=1_500_000, fail_burn_reads=(), stream_dies_when_job_ends=False, machine_age_s=5.0, machine_offset_s=37.0,
                 date_header="machine", burn_late_s=0.0, burn_bucket_s=1.0):
        """``burn_s``: how long the fake burn writes (None: the cap in its command line, 600 s); ``machine_offset_s``: how far the machine's clock is
        ahead of the driver's (it stamps the samples and the ``Date`` header of the 202); ``date_header``: ``machine`` | ``none`` | ``garbage``;
        ``burn_late_s``: the burn starts writing this long after its start command returned; ``burn_bucket_s``: how long one sample takes (1.0: no
        drift; burn.py's seconds drift under throttling)."""
        self.__dict__.update(locals())
        self.clock, self.events, self.asks, self.scheduled, self.popens = EPOCH, [], [], [], []
        self.burn_reads, self.snapshot_clock, self.job_ended_at = 0, [], None
        self.burn_started = self.burn_stopped = self.accepted = None
        self.burn_samples_path = self.burn_stop_path = self.burn_cap = None
        self.fly_calls, self.burn_start_returned = [], None          # every flyctl call as (clock when it was issued, kind)
        self.uploads, self.sampler_started_at, self.job_served = [], None, False
        self.stage = stage if stage is not None else stage_text()

    def machine_time(self, local=None):
        return (self.clock if local is None else local) + self.machine_offset_s

    # -- the clock ---------------------------------------------------------------------------------------------------------------
    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds
        for item in [s for s in self.scheduled if s[0] <= self.clock]:
            self.scheduled.remove(item)
            item[1]()
        time.sleep(0.003)                                      # let the log reader thread run

    # -- the log stream -------------------------------------------------------------------------------------------------------------
    def popen(self, args, **kwargs):
        self.events.append(("logs", "start"))
        rejected = self.logs_flag_rejected and "--machine" in args
        proc = FakePopen(args, self, exits_at_once=1 if rejected else None)
        self.popens.append(proc)
        if not rejected:
            proc.push(f"noise: GET /api/workspace/{WS_ID}/jobs/x origin {ORIGIN} admin {ADMIN}")
        return proc

    def deliver_stage_line(self):
        if self.in_stream:
            live = [p for p in self.popens if p.returncode is None]
            if live:
                live[-1].push(self.stage)

    # -- the service ---------------------------------------------------------------------------------------------------------------
    def handler(self, request):
        path, method = request.url.path, request.method
        self.events.append(("http", method, path))
        if path != "/healthz" and request.headers.get("x-origin-auth") != ORIGIN:
            return httpx.Response(403, json={"detail": "Forbidden"})
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "db": True, "embedder_variant": "fp32"})
        if path == "/api/admin/policy":
            return httpx.Response(200, json={"kill_switch": "off", "effective": "off"})
        if path == "/api/workspace" and method == "POST":
            return httpx.Response(201, json={"workspace_id": WS_ID, "token": WS_TOKEN, "expires_at": "later", "limits": {}})
        if path == f"/api/workspace/{WS_ID}/documents":
            self.accepted = self.clock
            self.uploads.append(request.content)
            headers = {"machine": {"date": formatdate(self.machine_time(), usegmt=True)}, "none": {}, "garbage": {"date": "not a date"}}[self.date_header]
            return httpx.Response(202, json={"job_id": JOB_ID, "document_id": "d", "version": 1}, headers=headers)
        if path == f"/api/workspace/{WS_ID}/jobs/{JOB_ID}":
            return self.job()
        if path == f"/api/workspace/{WS_ID}" and method == "DELETE":
            return httpx.Response(204)
        if path == "/api/ask":
            self.asks.append(request.content)
        return httpx.Response(404, json={"detail": "not found"})

    def job(self):
        events = [{"state": "queued"}, {"state": "embedding", "progress": {"done": 3, "total": 60, "eta_s": 10}}]
        if self.hang_job:
            self.clock += 400.0
        else:
            if not self.job_served:
                self.job_served = True
                self.clock += self.job_s
                self.job_ended_at = self.clock
                if self.stream_dies_when_job_ends:                  # flyctl logs ends by itself in the middle of the run
                    for proc in [p for p in self.popens if p.returncode is None]:
                        proc.returncode = 1
                        proc.queue.put(None)
                self.scheduled.append((self.clock + self.line_lag_s, self.deliver_stage_line))
                if self.line_lag_s == 0:
                    self.scheduled.pop()
                    self.deliver_stage_line()
            error = {"code": self.error_code, "message": "processing took too long and was stopped"} if self.job_final == "failed" else None
            events.append({"state": self.job_final, "error": error})
        body = "".join(f"event: job\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    # -- flyctl and the machine -----------------------------------------------------------------------------------------------------
    def machine_row(self, mid=MACHINE):
        created = datetime.fromtimestamp(self.clock - self.machine_age_s, UTC).isoformat().replace("+00:00", "Z")
        return {"id": mid, "state": "started", "region": "sin", "created_at": created, "config": {"guest": dict(GUEST)}}

    def burn_writing_since(self):
        return self.burn_started + self.burn_late_s

    def burn_available(self):
        if self.burn_started is None or self.burn_never_writes:
            return 0
        written = max(0, int((self.clock - self.burn_writing_since()) / self.burn_bucket_s))
        if self.burn_stopped is not None and not self.burn_ignores_stop:
            written = min(written, max(0, int((self.burn_stopped - self.burn_writing_since()) / self.burn_bucket_s)) + 1)
        return min(written, self.burn_s if self.burn_s is not None else self.burn_cap)

    def sampler_jsonl(self, count=None):
        start = self.sampler_started_at or self.clock
        header = {"header": {"t": start, "mem_total_kb": 4_000_000, "swap_total_kb": 1_048_576, "cpus": 2}}
        total = count or max(3, int((self.clock - start) // 10))
        rows = [header] + [{"t": start + i * 10, "mem_available_kb": 2_000_000, "swap_free_kb": 1_048_576, "swap_total_kb": 1_048_576, "uvicorn_pid": 11,
                            "vmrss_kb": 900_000, "vmhwm_kb": self.hwm if i > total // 2 else self.hwm - 200_000, "oom_kill": 0} for i in range(total)]
        return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()

    def __call__(self, args, timeout):
        words = args[1:]
        self.clock += 2.0                                       # an ssh connection costs a moment of the virtual clock
        self.fly_calls.append((self.clock - 2.0, words[words.index("-C") + 1] if "-C" in words else " ".join(words[:2])))
        if words[:2] == ["machine", "list"]:
            return w1b.Result(0, json.dumps([self.machine_row(f"m{i}") if i else self.machine_row() for i in range(self.machines)]).encode(), b"")
        if words[:2] == ["machine", "status"]:
            return w1b.Result(0, json.dumps({"id": MACHINE, "events": [{"type": "start"}, {"type": "launch"}]}).encode(), b"")
        if words[:1] == ["logs"]:
            self.events.append(("fly", "logs-snapshot"))
            self.snapshot_clock.append(self.clock)
            text = (self.stage + "\n") if self.in_snapshot else "all quiet\n"
            return w1b.Result(0, text.encode(), b"")
        command = words[words.index("-C") + 1]
        if command.startswith("python -c") and "/tmp/memsample.stop" in command:
            self.events.append(("fly", "sampler-start"))
            self.sampler_started_at = self.clock
            return w1b.Result(0 if self.sampler_ok else 1, b"", b"")
        if command.startswith("python -c") and "--stop-file" in command:
            self.events.append(("fly", "burn-start"))
            burn_words = shlex.split(command)
            self.burn_samples_path = burn_words[burn_words.index("--out") + 1] + "/samples.jsonl"
            self.burn_stop_path = burn_words[burn_words.index("--stop-file") + 1]
            self.burn_cap = int(burn_words[burn_words.index("--burn-s") + 1])
            self.burn_started = self.burn_start_returned = self.clock
            return w1b.Result(0, b"", b"")
        if self.burn_stop_path is not None and command == f"touch {self.burn_stop_path}":
            self.events.append(("fly", "burn-stop"))
            self.burn_stopped = self.burn_stopped or self.clock
            return w1b.Result(0, b"", b"")
        if command.startswith("tail -n +"):
            self.events.append(("fly", "burn-read"))
            self.burn_reads += 1
            if self.burn_reads in self.fail_burn_reads:
                return w1b.Result(255, b"", b"ssh: connection reset")
            if self.burn_started is None or self.burn_never_writes or command.split()[3] != self.burn_samples_path:
                return w1b.Result(1, b"", b"tail: cannot open: No such file or directory")
            first = int(command.split()[2].lstrip("+"))
            rate = lambda t: 1000.0 if self.onset is None or t < self.onset else 250.0  # noqa: E731
            lines = "".join(json.dumps({"t": t, "phase": "burn", "iter_per_s": rate(t), "steal_frac": None,
                                        "wall": self.machine_time(self.burn_writing_since() + t * self.burn_bucket_s)}) + "\n"
                            for t in range(first, self.burn_available() + 1))
            return w1b.Result(0, lines.encode(), b"")
        if command.startswith("tail"):
            return w1b.Result(0, self.sampler_jsonl(2) if self.sampler_ok else b"", b"")
        if command.startswith("touch"):
            self.events.append(("fly", "sampler-stop"))
            return w1b.Result(0, b"", b"")
        if command.startswith("cat"):
            return w1b.Result(0, self.sampler_jsonl(), b"")
        raise AssertionError(f"unexpected command {command[:60]!r}")


@pytest.fixture
def state_dir(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "W1d.json").write_text(json.dumps({"secrets": {"api": {"ORIGIN_AUTH_SECRET": ORIGIN, "ADMIN_TOKEN": ADMIN}}}), encoding="utf-8")
    return tmp_path / "state"


def drive(state_dir, tmp_path, world=None, extra=(), argv=None):
    """Run ``w1d.main`` for one boot against the fakes; returns (exit code, report or None, stdout, stderr, world)."""
    world = world or World()
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 tiny")
    out_file, out, err = tmp_path / "boot.json", io.StringIO(), io.StringIO()
    client = httpx.Client(transport=httpx.MockTransport(world.handler), timeout=httpx.Timeout(10.0))
    argv = argv or ["boot", "--boot", "1", "--state-dir", str(state_dir), "--pdf", str(tmp_path / "doc.pdf"), "--out", str(out_file), *extra]
    with redirect_stdout(out), redirect_stderr(err):
        code = w1d.main(argv, runner=world, popen=world.popen, client=client, now=world.now, sleep=world.sleep)
    report = json.loads(out.getvalue()) if out.getvalue().strip() and argv[0] == "boot" else None
    return code, report, out.getvalue(), err.getvalue(), world


# --- one boot, end to end ----------------------------------------------------------------------------------------------------------------

def test_a_throttled_boot_that_finishes_inside_the_ceiling_passes_and_carries_the_numbers(state_dir, tmp_path):
    code, report, out, err, world = drive(state_dir, tmp_path)
    assert code == 0 and report["verdict"] == "PASS" and report["reasons"] == [] and report["window"] == "W1d" and report["attempt"] == 1
    assert report["job"]["state"] == "ready" and report["job"]["job_id"] == JOB_ID and 1330 <= report["job"]["ready_s"] <= 1340
    assert report["job"]["end_s"] == report["job"]["ready_s"] and 0 <= report["job"]["burn_confirm_to_accept_local_s"] < 60
    assert report["stages"]["source"] == "stream" and report["stages"]["fields"]["embed_s"] == 1300.0 and report["stages"]["fields"]["tokens"] == 16000
    assert report["burn"]["stopped_by"] == "burn_ended" and report["burn"]["confirmed_stopped"] is True and report["burn"]["burn_complete"] is True
    assert report["burn"]["onset_s"] == 115 and report["burn"]["onset_after_floor"] is True and report["burn"]["samples"] == 600
    assert len(report["burn"]["per_second"]) == 600 and report["burn"]["burn_seconds"] == 600 and report["burn"]["waited_s"] >= 630
    assert report["workload"]["asks"] == 0 and report["workload"]["burn_s"] == 600 and report["workload"]["min_burn_s"] == 300
    assert report["max_vmhwm_kb"] == 1_500_000 and report["oom_or_restart"]["detected"] is False and report["healthz"]["boot_to_healthy_s"] is not None
    assert report["criteria"]["share_within_limit"] is True and report["embedder_variant"] == "fp32"
    assert json.loads((tmp_path / "boot.json").read_text(encoding="utf-8"))["verdict"] == "PASS"
    assert (tmp_path / "boot.logs.txt").read_text(encoding="utf-8").count("upload stages") == 1


def test_the_upload_follows_the_confirmation_at_once_and_the_gap_is_recorded_on_the_drivers_clock(state_dir, tmp_path):
    _, report, *_ = drive(state_dir, tmp_path)
    job, burn = report["job"], report["burn"]
    assert job["burn_confirm_to_accept_local_s"] == 0.0                                  # the driver's clock twice: the end of the confirming read, the acceptance
    assert burn["last_sample_wall_machine"] is not None
    assert "confirmed_at" not in burn and "accepted_at" not in job and "accepted_machine_at" not in job          # raw epochs of either clock are not reported
    assert not {"burn_end_to_accept_s", "burn_stop_to_accept_s", "burn_last_sample_to_accept_s"} & set(job) and "last_sample_wall" not in burn


@pytest.mark.parametrize("offset", [0.0, 37.0, -400.0])
def test_the_gap_on_the_machines_clock_is_the_machines_last_sample_to_the_machines_date_header_whatever_the_offset_between_the_clocks(state_dir, tmp_path, offset):
    code, report, _, _, world = drive(state_dir, tmp_path, World(machine_offset_s=offset))
    job = report["job"]
    # the burn started when its start command returned and wrote its last sample 600 s later; the 202 came after the wait, two reads and the gap
    expected = world.accepted - (world.burn_started + 600)
    assert expected > 30 and abs(job["burn_last_sample_to_accept_machine_clock_s"] - expected) <= 1.5       # the Date header has a resolution of one second
    assert report["burn"]["last_sample_wall_machine"] == pytest.approx(world.machine_time(world.burn_started + 600))


@pytest.mark.parametrize("header", ["none", "garbage"])
def test_a_missing_or_unreadable_date_header_leaves_the_machine_clock_gap_null_and_never_borrows_the_drivers_clock(state_dir, tmp_path, header):
    code, report, *_ = drive(state_dir, tmp_path, World(date_header=header))
    assert report["job"]["state"] == "ready" and report["job"]["burn_last_sample_to_accept_machine_clock_s"] is None
    assert report["job"]["burn_confirm_to_accept_local_s"] == 0.0                         # the driver's own gap is still there


def test_no_ask_is_ever_sent_and_the_upload_is_the_only_load(state_dir, tmp_path):
    _, _, _, _, world = drive(state_dir, tmp_path)
    assert world.asks == [] and not [e for e in world.events if e[0] == "http" and e[2] == "/api/ask"]
    assert len(world.uploads) == 1


def test_the_order_logs_then_sampler_then_burn_then_the_burn_is_read_and_confirmed_over_and_only_then_the_upload(state_dir, tmp_path):
    _, report, _, _, world = drive(state_dir, tmp_path)
    ev = world.events
    first = lambda e: ev.index(e)  # noqa: E731
    assert first(("logs", "start")) < first(("fly", "sampler-start")) < first(("fly", "burn-start")) < first(("fly", "burn-read"))
    assert first(("fly", "burn-read")) < first(("http", "POST", "/api/workspace")) < first(("http", "POST", f"/api/workspace/{WS_ID}/documents"))
    assert first(("http", "POST", f"/api/workspace/{WS_ID}/documents")) < first(("logs", "stop")) < first(("fly", "sampler-stop"))
    assert ev.count(("fly", "burn-start")) == 1 and ev.count(("fly", "burn-stop")) == 0 and ev.count(("logs", "stop")) == 1       # nothing to stop: it ended itself
    assert ev.count(("fly", "burn-read")) == 2                                                 # the whole file once, one read that finds nothing new
    assert max(i for i, e in enumerate(ev) if e == ("fly", "burn-read")) < min(i for i, e in enumerate(ev) if e[:2] == ("http", "POST") and "/documents" in e[2])


def test_no_flyctl_call_of_any_kind_is_made_while_the_burn_can_still_be_running(state_dir, tmp_path):
    _, report, _, _, world = drive(state_dir, tmp_path)
    quiet_from, quiet_until = world.burn_start_returned, world.burn_start_returned + 600 + w1d_burn.MARGIN_S
    inside = [(at, command[:50]) for at, command in world.fly_calls if quiet_from <= at < quiet_until]
    assert inside == [], inside                                                            # the machine starves sshd and hallpass while the burn runs: no ssh at all
    after = [command for at, command in world.fly_calls if at >= quiet_until]
    assert after and after[0].startswith("tail -n +1 ")                                    # the first thing after the wait is the read of the whole file


def test_the_log_stream_is_flyctl_logs_of_the_registered_app_on_the_machine_and_is_stopped_once(state_dir, tmp_path):
    _, _, _, _, world = drive(state_dir, tmp_path)
    assert len(world.popens) == 1 and world.popens[0].args == ["flyctl", "logs", "-a", APP, "--machine", MACHINE]
    assert world.popens[0].terminated == 1


def test_a_flyctl_that_rejects_machine_gets_the_app_wide_stream(state_dir, tmp_path):
    _, report, _, _, world = drive(state_dir, tmp_path, World(logs_flag_rejected=True))
    assert [p.args for p in world.popens] == [["flyctl", "logs", "-a", APP, "--machine", MACHINE], ["flyctl", "logs", "-a", APP]]
    assert report["verdict"] == "PASS" and world.popens[1].terminated == 1


def test_a_stream_that_dies_at_once_both_ways_is_reported_and_the_snapshot_is_used(state_dir, tmp_path):
    class Dead(World):
        def popen(self, args, **kwargs):
            self.events.append(("logs", "start"))
            proc = FakePopen(args, self, exits_at_once=1)
            self.popens.append(proc)
            return proc

    _, report, _, _, world = drive(state_dir, tmp_path, Dead(in_snapshot=True))
    assert report["stages"]["source"] == "snapshot" and report["stages"]["stream_error"] and report["verdict"] == "PASS"


# --- the stage line: late, in the snapshot, nowhere --------------------------------------------------------------------------------------------

def test_a_line_that_arrives_after_ready_is_waited_for(state_dir, tmp_path):
    _, report, _, _, world = drive(state_dir, tmp_path, World(line_lag_s=20.0))
    assert report["stages"]["source"] == "stream" and report["verdict"] == "PASS"


def test_a_line_that_never_reaches_the_stream_is_read_from_the_snapshot(state_dir, tmp_path):
    _, report, _, _, world = drive(state_dir, tmp_path, World(in_stream=False, in_snapshot=True))
    assert report["stages"]["source"] == "snapshot" and report["verdict"] == "PASS"
    assert world.events.count(("fly", "logs-snapshot")) == 2                       # the early one serves the stage line, the final one the evidence


def test_no_line_anywhere_is_inconclusive_never_a_pass(state_dir, tmp_path):
    code, report, *_ = drive(state_dir, tmp_path, World(in_stream=False, in_snapshot=False))
    assert code == 2 and report["verdict"] == "INCONCLUSIVE" and report["stages"]["source"] is None and report["stages"]["fields"] is None
    assert any("upload stages" in reason for reason in report["reasons"])


def test_a_line_of_another_job_is_not_this_jobs_line(state_dir, tmp_path):
    other = stage_text().replace(JOB_ID, "someotherjob99")
    code, report, *_ = drive(state_dir, tmp_path, World(stage=other))
    assert code == 2 and report["verdict"] == "INCONCLUSIVE"


# --- the verdicts through the driver -------------------------------------------------------------------------------------------------------------

def test_a_job_that_ran_fast_is_invalid_and_exits_2(state_dir, tmp_path):
    world = World(job_s=430.0, stage=stage_text(embed_s=400.0))
    code, report, *_ = drive(state_dir, tmp_path, world)
    assert code == 2 and report["verdict"] == "INVALID" and any("above 0.15" in r for r in report["reasons"])


def test_a_job_stopped_by_the_ceiling_while_valid_is_a_fail_and_exits_1(state_dir, tmp_path):
    stage = stage_text(embed_s=2101.5, done=41)
    world = World(job_s=2125.0, job_final="failed", error_code="timeout", stage=stage)
    code, report, *_ = drive(state_dir, tmp_path, world)
    assert code == 1 and report["verdict"] == "FAIL" and report["job"]["state"] == "failed" and report["job"]["ready_s"] is None
    assert 2125 <= report["job"]["end_s"] <= 2135 and report["job"]["error"]["code"] == "timeout"


def test_a_memory_peak_over_the_limit_fails(state_dir, tmp_path):
    code, report, *_ = drive(state_dir, tmp_path, World(hwm=2_400_001))
    assert code == 1 and report["verdict"] == "FAIL" and report["max_vmhwm_kb"] == 2_400_001


def test_the_harness_deadline_is_inconclusive_not_a_fail(state_dir, tmp_path):
    code, report, *_ = drive(state_dir, tmp_path, World(hang_job=True))
    assert code == 2 and report["verdict"] == "INCONCLUSIVE" and report["job"]["state"] == "timeout"


def test_a_burn_that_finds_no_onset_still_sends_the_upload_and_the_rule_decides(state_dir, tmp_path):
    world = World(onset=None, job_s=430.0, stage=stage_text(embed_s=400.0))
    code, report, *_ = drive(state_dir, tmp_path, world)
    assert report["burn"]["stopped_by"] == "burn_ended" and report["burn"]["samples"] == 600 and report["job"]["state"] == "ready"
    assert report["verdict"] == "INVALID" and code == 2                                # not throttled: the validity rule says so, the burn does not decide


# --- the burn must be over, and have run its full length, before the upload ---------------------------------------------------------------------------

def no_post(world):
    return world.uploads == [] and not [e for e in world.events if e[:2] == ("http", "POST")]


def test_a_complete_600_line_file_is_confirmed_and_the_upload_follows_with_no_stop_file(state_dir, tmp_path):
    code, report, _, _, world = drive(state_dir, tmp_path)
    assert code == 0 and len(world.uploads) == 1 and report["burn"]["burn_seconds"] == 600 and report["burn"]["read_failures"] == 0
    assert world.events.count(("fly", "burn-stop")) == 0 and world.burn_stopped is None


@pytest.mark.parametrize("length", [450, 301, 100])
def test_a_short_file_the_burn_died_is_confirmed_stopped_but_incomplete_so_no_upload_and_the_boot_is_inconclusive(state_dir, tmp_path, length):
    code, report, _, _, world = drive(state_dir, tmp_path, World(burn_s=length))          # 450 and 301 are above the 300 s floor: only the completeness rule stops these
    assert code == 2 and report["verdict"] == "INCONCLUSIVE" and report["job"]["state"] == "not_started"
    assert report["burn"]["confirmed_stopped"] is True and report["burn"]["burn_complete"] is False and report["burn"]["stopped_by"] == "burn_died"
    assert report["burn"]["burn_seconds"] == length and "full 600 s" in report["job"]["error"] and str(length) in report["job"]["error"]
    assert no_post(world) and world.popens[0].terminated == 1                              # the stream is stopped on this path too


@pytest.mark.parametrize("ignores", [True, False], ids=["past-its-cap-and-the-stop-file-is-ignored", "far-behind-stopped-short-by-the-stop-file"])
def test_a_file_still_growing_after_the_margin_and_the_extra_waits_gets_the_stop_file_and_if_that_fails_or_the_burn_is_short_no_upload_follows(state_dir, tmp_path, ignores):
    world = World(burn_s=1500, burn_ignores_stop=True) if ignores else World(burn_bucket_s=2.0)
    code, report, _, _, world = drive(state_dir, tmp_path, world)
    assert code == 2 and report["verdict"] == "INCONCLUSIVE" and report["job"]["state"] == "not_started" and report["burn"]["stopped_by"] == "stop_file"
    assert report["burn"]["confirmed_stopped"] is (not ignores) and world.events.count(("fly", "burn-stop")) == 1 and no_post(world)
    touch_at = next(at for at, command in world.fly_calls if command.startswith("touch /tmp/w1d-burn-"))
    assert touch_at >= world.burn_start_returned + 600 + w1d_burn.MARGIN_S                  # the fallback comes after the margin, never while the burn runs


def test_a_file_still_growing_past_the_margin_that_obeys_the_stop_file_has_run_its_full_length_so_the_upload_follows(state_dir, tmp_path):
    code, report, _, _, world = drive(state_dir, tmp_path, World(burn_s=1500))
    assert report["burn"]["stopped_by"] == "stop_file" and report["burn"]["confirmed_stopped"] is True and report["burn"]["burn_complete"] is True
    assert len(world.uploads) == 1 and report["job"]["state"] == "ready" and code == 0


def test_a_burn_whose_seconds_drift_is_waited_for_locally_and_the_upload_follows_with_no_stop_file(state_dir, tmp_path):
    code, report, _, _, world = drive(state_dir, tmp_path, World(burn_bucket_s=1.10))     # 600 samples take 660 s: still writing when the 30 s margin ends
    assert code == 0 and report["verdict"] == "PASS" and len(world.uploads) == 1
    assert report["burn"]["stopped_by"] == "burn_ended" and report["burn"]["burn_seconds"] == 600 and world.events.count(("fly", "burn-stop")) == 0
    reads = [at for at, command in world.fly_calls if command.startswith("tail -n +")]
    assert len(reads) == 4 and not [c for at, c in world.fly_calls if reads[1] + 2.0 <= at < reads[2]]      # nothing between the read that saw it grow and the catch-up
    assert abs(report["burn"]["burn_span_machine_s"] - 599 * 1.1) < 1e-6 and len(report["burn"]["reads"]) == 4
    assert report["job"]["burn_last_sample_to_accept_machine_clock_s"] is not None and report["job"]["burn_confirm_to_accept_local_s"] == 0.0


def test_a_burn_that_never_wrote_is_not_started_and_never_followed_by_an_upload(state_dir, tmp_path):
    code, report, _, _, world = drive(state_dir, tmp_path, World(burn_never_writes=True))
    assert code == 2 and report["verdict"] == "INCONCLUSIVE" and report["job"]["state"] == "not_started"
    assert "burn" in report["job"]["error"] and report["burn"]["confirmed_stopped"] is False and report["burn"]["stopped_by"] == "not_started"
    assert no_post(world) and world.popens[0].terminated == 1


def test_reads_that_fail_after_the_burn_are_retried_and_the_upload_follows_once_one_works(state_dir, tmp_path):
    code, report, _, _, world = drive(state_dir, tmp_path, World(fail_burn_reads=(1, 2)))
    assert code == 0 and report["verdict"] == "PASS" and len(world.uploads) == 1
    assert report["burn"]["stopped_by"] == "burn_ended" and report["burn"]["burn_seconds"] == 600 and report["burn"]["read_failures"] == 2
    assert world.events.count(("fly", "burn-start")) == 1 and world.events.count(("fly", "burn-stop")) == 0


def test_reads_that_keep_failing_after_the_burn_end_inconclusive_with_no_upload(state_dir, tmp_path):
    code, report, _, _, world = drive(state_dir, tmp_path, World(fail_burn_reads=set(range(1, 1000))))
    assert code == 2 and report["verdict"] == "INCONCLUSIVE" and report["job"]["state"] == "not_started"
    assert report["burn"]["stopped_by"] == "read_failed" and report["burn"]["confirmed_stopped"] is False and report["burn"]["read_failures"] >= 10
    assert "could not be confirmed stopped" in report["job"]["error"] and no_post(world) and world.popens[0].terminated == 1
    assert world.events.count(("fly", "burn-stop")) == 0                                    # a failing ssh is not asked for a stop file; the burn ended at its own cap


def test_the_minimum_of_300_s_is_still_checked_after_the_completeness_rule(state_dir, tmp_path, monkeypatch):
    def claims_complete(fly, **kwargs):
        return {"started": True, "stopped_by": "burn_ended", "confirmed_stopped": True, "burn_complete": True, "burn_seconds": 250, "confirmed_at": 1.0,
                "last_sample_wall_machine": 2.0, "samples": 250, "elapsed_s": 1.0}

    monkeypatch.setattr(w1d_burn, "run_burn_step", claims_complete)
    code, report, _, _, world = drive(state_dir, tmp_path)
    assert code == 2 and report["job"]["state"] == "not_started" and "minimum 300 s" in report["job"]["error"] and "250" in report["job"]["error"]
    assert no_post(world)


# --- the stage line: an early snapshot when the stream has nothing -----------------------------------------------------------------------------------

def test_a_stream_with_no_line_gets_a_snapshot_as_soon_as_the_job_ends_before_the_stream_and_the_sampler_are_stopped(state_dir, tmp_path):
    _, report, _, _, world = drive(state_dir, tmp_path, World(in_stream=False, in_snapshot=True))
    ev = world.events
    assert report["stages"]["source"] == "snapshot" and report["verdict"] == "PASS"
    assert ev.count(("fly", "logs-snapshot")) == 2                                      # the early one for the line, the final one for the evidence
    assert ev.index(("fly", "logs-snapshot")) < ev.index(("logs", "stop")) < ev.index(("fly", "sampler-stop"))


def test_a_stream_that_died_in_the_middle_of_the_run_gets_the_early_snapshot_without_waiting_out_the_log_wait(state_dir, tmp_path):
    world = World(stream_dies_when_job_ends=True, in_snapshot=True)
    _, report, _, _, world = drive(state_dir, tmp_path, world)
    ev = world.events
    assert report["stages"]["source"] == "snapshot" and report["verdict"] == "PASS"
    assert ev.count(("fly", "logs-snapshot")) == 2 and ev.index(("fly", "logs-snapshot")) < ev.index(("fly", "sampler-stop"))
    assert world.snapshot_clock[0] - world.job_ended_at < w1d.LOG_WAIT_S                  # no wait for a stream that is not running
    _, _, _, _, silent = drive(state_dir, tmp_path, World(in_stream=False, in_snapshot=True))
    assert silent.snapshot_clock[0] - silent.job_ended_at >= w1d.LOG_WAIT_S                # a stream that runs and says nothing is waited out first


def test_a_line_that_reached_the_stream_needs_no_early_snapshot(state_dir, tmp_path):
    _, report, _, _, world = drive(state_dir, tmp_path)
    assert report["stages"]["source"] == "stream" and world.events.count(("fly", "logs-snapshot")) == 1


def test_a_line_in_neither_the_stream_nor_either_snapshot_is_still_inconclusive(state_dir, tmp_path):
    code, report, _, _, world = drive(state_dir, tmp_path, World(stream_dies_when_job_ends=True))
    assert code == 2 and report["verdict"] == "INCONCLUSIVE" and report["stages"]["source"] is None and world.events.count(("fly", "logs-snapshot")) == 2


# --- secrets and scrubbing ---------------------------------------------------------------------------------------------------------------------------

def test_no_secret_reaches_stdout_stderr_the_files_or_any_flyctl_argument(state_dir, tmp_path):
    world = World()
    seen = []
    original = world.__call__

    def spy(args, timeout):
        seen.append(list(args))
        return original(args, timeout)

    world.__class__ = type("Spy", (World,), {"__call__": lambda self, args, timeout: spy(args, timeout)})
    _, _, out, err, world = drive(state_dir, tmp_path, world)
    files = (tmp_path / "boot.json").read_text(encoding="utf-8") + (tmp_path / "boot.logs.txt").read_text(encoding="utf-8")
    assert "[redacted]" in files                                                       # the stream carried the workspace id and both secrets
    for secret in (ORIGIN, ADMIN, WS_TOKEN, WS_ID):
        assert secret not in out and secret not in err and secret not in files, secret[:18]
        assert not any(secret in word for call in seen for word in call) and not any(secret in " ".join(p.args) for p in world.popens)


def test_the_stream_is_stopped_once_even_when_the_boot_breaks_in_the_middle(state_dir, tmp_path):
    code, report, _, _, world = drive(state_dir, tmp_path, World(sampler_ok=False))
    assert code == 2 and report["job"]["state"] == "not_started" and "sampler" in report["job"]["error"]
    assert world.popens[0].terminated == 1 and world.uploads == []


# --- guards ---------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [["--burn-s", "300"], ["--min-burn-s", "100"], ["--burn-s", "900"]])
def test_the_registered_burn_numbers_cannot_be_changed_from_the_command_line(state_dir, tmp_path, extra):
    code, report, _, err, world = drive(state_dir, tmp_path, extra=extra)
    assert code == 2 and report is None and "W1d registers" in err and world.events == [] and world.popens == []


def test_the_registered_numbers_are_the_default_and_spelling_them_out_is_allowed(state_dir, tmp_path):
    code, report, *_ = drive(state_dir, tmp_path, extra=["--burn-s", "600", "--min-burn-s", "300"])
    assert code == 0 and report["workload"]["burn_s"] == 600


def test_the_window_state_file_is_required_and_named_in_the_error(state_dir, tmp_path):
    (state_dir / "W1d.json").unlink()
    code, report, _, err, world = drive(state_dir, tmp_path)
    assert code == 2 and "staging.py create W1d" in err and world.events == []


def test_two_machines_are_refused_before_anything_is_started(state_dir, tmp_path):
    code, report, _, err, world = drive(state_dir, tmp_path, World(machines=2))
    assert code == 2 and "exactly one machine" in err and world.popens == [] and world.uploads == []


def test_the_target_must_be_the_registered_staging_app_never_the_live_one(state_dir, tmp_path):
    code, _, _, err, world = drive(state_dir, tmp_path, extra=["--base-url", "https://semigraph.fly.dev"])
    assert code == 2 and "target must be" in err and world.events == []


def test_only_boot_1_exists_and_attempt_2_is_recorded(state_dir, tmp_path):
    with pytest.raises(SystemExit):
        w1d.main(["boot", "--boot", "2"])
    with pytest.raises(SystemExit):
        w1d.main(["boot", "--boot", "1", "--attempt", "3"])
    code, report, *_ = drive(state_dir, tmp_path, extra=["--attempt", "2"])
    assert code == 0 and report["attempt"] == 2 and report["boot"] == 1


def test_the_harness_deadline_follows_the_jobs_own_budgets_from_the_code_defaults():
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.config import Settings
    parse, embed, compare = (Settings.model_fields[f"upload_{k}_timeout_s"].default for k in ("parse", "embed", "compare"))
    assert (parse, embed, compare) == (90, 2100, 120) and embed == w1d_judge.EMBED_CEILING_S
    assert w1d.JOB_TIMEOUT_S >= parse + embed + w1d.LAST_CHUNK_S + compare and w1d.JOB_TIMEOUT_S == 2680
    assert w1d.JOB_TIMEOUT_S > w1b.JOB_TIMEOUT_S                                          # W1b's 1,470 s would end the watch in the middle of a valid job


def test_the_sampler_outlives_the_burn_and_the_longest_job(state_dir, tmp_path):
    seen = []
    world = World()
    original = world.__call__
    world.__class__ = type("Spy", (World,), {"__call__": lambda self, args, timeout: (seen.append(list(args)), original(args, timeout))[1]})
    drive(state_dir, tmp_path, world)
    command = next(a[a.index("-C") + 1] for a in seen if "-C" in a and "/tmp/memsample.stop" in a[a.index("-C") + 1] and a[a.index("-C") + 1].startswith("python"))
    duration = int(re.search(r"--duration-s (\d+)$", command).group(1))
    assert duration >= 600 + w1d.JOB_TIMEOUT_S


# --- aggregate --------------------------------------------------------------------------------------------------------------------------------------------

def test_aggregate_judges_the_boot_files_and_exits_by_the_verdict(state_dir, tmp_path):
    _, passing, *_ = drive(state_dir, tmp_path)
    one = tmp_path / "boot-1.json"
    one.write_text(json.dumps(passing), encoding="utf-8")
    code, _, out, _, _ = drive(state_dir, tmp_path, argv=["aggregate", str(one), "--out", str(tmp_path / "w1d.json")])
    assert code == 0 and json.loads(out)["verdict"] == "PASS" and json.loads((tmp_path / "w1d.json").read_text(encoding="utf-8"))["window"] == "W1d"
    fast = World(job_s=430.0, stage=stage_text(embed_s=400.0))
    _, invalid, *_ = drive(state_dir, tmp_path, fast)
    one.write_text(json.dumps(invalid), encoding="utf-8")
    code, _, out, _, _ = drive(state_dir, tmp_path, argv=["aggregate", str(one)])
    result = json.loads(out)
    assert code == 2 and result["verdict"] == "INCOMPLETE" and result["rerun_due"] is True
    failing = World(job_s=2125.0, job_final="failed", stage=stage_text(embed_s=2101.5, done=41))
    _, fail, *_ = drive(state_dir, tmp_path, failing)
    one.write_text(json.dumps(fail), encoding="utf-8")
    code, _, out, _, _ = drive(state_dir, tmp_path, argv=["aggregate", str(one)])
    assert code == 1 and json.loads(out)["verdict"] == "FAIL"


def test_aggregate_never_judges_a_w1c_or_w1b_file_as_w1d(tmp_path):
    other = tmp_path / "boot-1.json"
    other.write_text(json.dumps({"window": "W1c", "boot": 1, "attempt": 1, "verdict": "PASS"}), encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out):
        code = w1d.main(["aggregate", str(other)])
    assert code == 2 and json.loads(out.getvalue())["verdict"] == "INCOMPLETE"


# --- the log stream on its own ------------------------------------------------------------------------------------------------------------------------

def test_the_log_stream_refuses_an_app_that_is_not_the_registered_one(tmp_path):
    world = World()
    stream = w1d.LogStream(w1b.Fly("semigraph", world), tmp_path / "l.txt", w1d.Scrub(), popen=world.popen, sleep=world.sleep)
    with pytest.raises(w1d.W1bError):
        stream.start(MACHINE)
    assert world.popens == []


def test_the_log_stream_scrubs_before_writing_and_stop_is_idempotent(tmp_path):
    world, scrub = World(), w1d.Scrub()
    scrub.add(ORIGIN)
    stream = w1d.LogStream(w1b.Fly(APP, world), tmp_path / "l.txt", scrub, popen=world.popen, sleep=world.sleep)
    stream.start(MACHINE)
    world.popens[0].push(f"secret {ORIGIN} here")
    for _ in range(50):
        world.sleep(0.1)
        if "here" in stream.text():
            break
    stream.stop()
    stream.stop()
    text = (tmp_path / "l.txt").read_text(encoding="utf-8")
    assert ORIGIN not in text and "secret [redacted] here" in text and world.popens[0].terminated == 1


def test_the_main_thread_count_is_back_to_normal_after_a_boot(state_dir, tmp_path):
    drive(state_dir, tmp_path)
    assert [t for t in threading.enumerate() if t.name.startswith("w1d-logs")] == []


# --- the documents: the rules and the whole sequence are stated where the operator reads them ----------------------------------------------------

WINDOWS = json.loads((ROOT / "deploy" / "staging" / "windows.json").read_text(encoding="utf-8"))


def squash(text):
    return " ".join(text.split())


def test_w1d_is_registered_with_the_upload_only_workload_the_driver_reads():
    entry = WINDOWS["windows"]["W1d"]
    assert w1b.load_registry(window="W1d") == {"api_app": APP, "workload": entry["workload"]}
    assert entry["workload"] == {"mode": "upload_only", "asks": 0, "burn_s": w1d_burn.DEFAULT_BURN_S, "min_burn_s": w1d_burn.DEFAULT_MIN_BURN_S,
                                 "onset": "below 90% of the baseline for 5 s in a row (tools/probe/report.py)"}
    assert "workload" not in WINDOWS["windows"]["W1b"] and "W1d" not in w1b.WINDOW


def test_the_readme_and_windows_json_state_the_pre_registered_rules_with_the_numbers_of_the_code():
    readme = squash((ROOT / "tools" / "probe" / "README.md").read_text(encoding="utf-8"))
    note = squash(WINDOWS["windows"]["W1d"]["note"])
    needles = ("85.8 x 1.18 x", f"{float(w1d_judge.SHARE_LIMIT)}", f"{float(w1d_judge.EMBED_READY_RATIO)} x", f"{w1d_judge.VMHWM_LIMIT_KB:,} kB",
               f"{w1d.JOB_TIMEOUT_S:,}", f"{w1d_judge.EMBED_CEILING_S:,} s", "300 s", "600 s", "confirmed stopped", "ready_s",
               "an INVALID boot is re-run unchanged ONCE on a fresh machine", "two INVALID boots end the window INCOMPLETE and nothing is amended",
               "upload stages", "NO ask", "mockllm")
    for text in (readme, note):
        for needle in needles:
            assert needle in text or needle.replace("NO ask", "no asks") in text, needle
    assert "$0.15" in readme and "quote $0.15" in note and "cap $0.25" in note


def test_the_readme_and_windows_json_state_that_the_burn_is_never_steered_over_ssh_and_that_the_early_stop_is_dropped_with_the_live_evidence():
    readme = squash((ROOT / "tools" / "probe" / "README.md").read_text(encoding="utf-8"))
    readme = readme[readme.index("## `w1d.py`"):]
    note = squash(WINDOWS["windows"]["W1d"]["note"])
    steps = squash(" ".join(WINDOWS["windows"]["W1d"]["steps"]))
    needles = ("never steered over ssh", "starves sshd", "the early stop on the onset is DROPPED", "sample 223", "waits locally", "630 s", "the whole samples file",
               "no new line", "fallback", "per-run", "an unconfirmed stop sends no upload", "Plan 2", "drift", "10 minutes")
    for text in (readme, note):
        for needle in needles:
            assert needle in text, needle
    for needle in ("burn_confirm_to_accept_local_s", "burn_last_sample_to_accept_machine_clock_s", "Date", "burn_span_machine_s", "burn.reads", "$canary.burn.stopped_by"):
        assert needle in readme, needle
    for needle in ("never steered over ssh", "630 s", "15 to 20 minutes", "$0.02", "up to about 65", "burn.stopped_by", "burn_ended", "burn.reads"):
        assert needle in steps, needle
    assert "fresh window" in note                                                          # the cost figure is end to end for a window created from scratch, not this window's balance
    for needle in ("15 to 20 minutes", "$0.02", "up to about 65"):
        assert needle in readme, needle
    # the old early stop and the old durations are gone from every text the operator reads
    for text in (readme, note, steps):
        for stale in ("300 to 600 s", "10 to 15 minutes", "$0.015", "50 to 60 minutes", "is stopped early once", "creates the stop file, then READS"):
            assert stale not in text, stale
    assert "should_stop" not in readme and "criterion_met" not in readme


def test_the_readme_and_windows_json_name_the_whole_w1d_sequence_and_deploy_no_mock_llm():
    readme = squash((ROOT / "tools" / "probe" / "README.md").read_text(encoding="utf-8"))
    steps = squash(" ".join(WINDOWS["windows"]["W1d"]["steps"]))
    for text in (readme, steps):
        for needle in ("staging.py quote W1d", "create W1d --org", "seed W1d", "deploy W1d api --build-arg EMBEDDER_VARIANT=fp32", "preflight W1d",
                       "machine destroy", "tools.probe.w1d boot", "--attempt 2", "tools.probe.w1d aggregate", "snapshot W1d", "destroy W1d --yes",
                       "--wait-for-machine-s 2700", "approve-quote 0.15", "boot-1.json"):
            assert needle in text, needle
        assert "deploy W1d mockllm" not in text and "KEEP_UNPATCHED=1" in text.replace("NOT KEEP_UNPATCHED=1", "KEEP_UNPATCHED=1")
    assert steps.index("START the driver BEFORE the deploy") < steps.rindex("scripts/staging.py deploy W1d api")
    # the stop rule is in the commands, not left to the operator: only an INVALID boot is re-run (decided from its JSON), once, and the aggregate gets every boot file
    assert '$first.verdict -eq "INVALID"' in readme and 'Test-Path "$dir\\boot-1-a2.json"' in readme
    assert 'Get-ChildItem "$dir\\boot-*.json"' in readme and "aggregate $files" in readme
    assert "only a boot whose JSON verdict is INVALID" in steps.replace("Only", "only") and "boot-*.json" in steps and "no attempt can be left out" in steps
    assert set(re.findall(r"--wait-for-machine-s (\d+)", readme + steps)) == {"2700"}


def test_the_runbook_has_a_canary_on_the_warm_up_machine_before_the_measured_boot_and_it_cannot_reach_the_aggregate():
    readme = squash((ROOT / "tools" / "probe" / "README.md").read_text(encoding="utf-8"))
    readme = readme[readme.index("## `w1d.py`"):]
    steps = squash(" ".join(WINDOWS["windows"]["W1d"]["steps"]))
    for text in (readme, steps):
        for needle in ("canary", "n_pages=2, chars_per_page=2700", "--pdf", "stages.source", "stages.fields", "warm-up machine", "small.pdf"):
            assert needle in text, needle
        # after the warm-up deploy and its preflight (the only machine), before the measured boot's destroy-and-redeploy
        assert text.index("preflight W1d") < text.index("canary.json") < text.index("--wait-for-machine-s 2700")
    assert 'tools.probe.w1d boot --boot 1 --pdf' in readme and '--out "$dir\\canary.json"' in readme and "tools.probe.w1d boot --boot 1 --pdf" in steps
    assert "--out <dir>/canary.json" in steps and "stream" in steps and "snapshot" in steps and "INVALID" in steps
    canary_command = re.search(r"tools\.probe\.w1d boot --boot 1 --pdf[^#]*?canary\.json\"", readme).group(0)
    assert "--wait-for-machine-s" not in canary_command and "--attempt" not in canary_command          # the machine exists now; it is not a numbered attempt
    # the aggregate's glob is boot-*.json: neither the canary's result nor its log file may match it
    import fnmatch
    assert not fnmatch.fnmatch("canary.json", "boot-*.json") and not fnmatch.fnmatch("canary.logs.txt", "boot-*.json")
    assert 'Get-ChildItem "$dir\\boot-*.json"' in readme


def test_the_canary_run_on_an_old_warm_up_machine_burns_its_floor_uploads_once_and_ends_invalid_with_a_stage_line_not_a_traceback(state_dir, tmp_path):
    """The canary's shape: the warm-up machine was created two hours ago and is healthy, no --wait-for-machine-s, a tiny job, the result in canary.json."""
    small = World(machine_age_s=7200.0, job_s=30.0, stage=stage_text(embed_s=5.0, tokens=1400))
    argv = ["boot", "--boot", "1", "--state-dir", str(state_dir), "--pdf", str(tmp_path / "doc.pdf"), "--out", str(tmp_path / "canary.json")]
    code, report, _, _, world = drive(state_dir, tmp_path, small, argv=argv)
    assert code == 2 and report["verdict"] == "INVALID" and report["window"] == "W1d"                    # a 2-page job is not throttled: expected, not a result
    assert report["healthz"]["boot_to_healthy_s"] is not None and report["healthz"]["boot_to_healthy_s"] > 7000        # an old machine is healthy on the first poll
    assert report["burn"]["burn_seconds"] == 600 and report["burn"]["confirmed_stopped"] is True and len(world.uploads) == 1
    assert report["stages"]["source"] == "stream" and report["stages"]["fields"] is not None and report["stages"]["fields"]["tokens"] == 1400
    assert (tmp_path / "canary.json").is_file() and (tmp_path / "canary.logs.txt").is_file() and not (tmp_path / "boot-1.json").exists()
    import fnmatch
    assert not [n for n in ("canary.json", "canary.logs.txt") if fnmatch.fnmatch(n, "boot-*.json")]


def test_the_canary_command_builds_the_small_pdf_the_fixture_makes(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("canary_fixtures", ROOT / "tests" / "upload_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    small = module.max_size_pdf(n_pages=2, chars_per_page=2700)
    assert small.startswith(b"%PDF") and 0 < len(small) < len(module.max_size_pdf(n_pages=30, chars_per_page=2700))
    readme = (ROOT / "tools" / "probe" / "README.md").read_text(encoding="utf-8")
    assert "import tests.upload_fixtures as f" in readme and "f.max_size_pdf(n_pages=2, chars_per_page=2700)" in readme


def test_the_new_text_files_are_ascii_with_lf_line_ends():
    for rel in ("tools/probe/w1d.py", "tools/probe/w1d_burn.py", "tools/probe/w1d_judge.py", "tools/probe/burn.py", "tools/probe/README.md",
                "deploy/staging/windows.json", "tests/test_tools_probe_w1d.py", "tests/test_tools_probe_w1d_burn.py", "tests/test_tools_probe_w1d_judge.py"):
        data = (ROOT / rel).read_bytes()
        assert b"\r" not in data and all(byte < 128 for byte in data), rel


def test_the_new_probe_modules_stay_under_the_soft_file_size_ceiling():
    for rel in ("tools/probe/w1d.py", "tools/probe/w1d_burn.py", "tools/probe/w1d_judge.py"):
        assert len((ROOT / rel).read_text(encoding="utf-8").splitlines()) < 800, rel

"""Locust glue for the S2 gate run (M5_PLAN.md section 6). The only module that imports locust, which monkey-patches gevent into
the process: nothing else in ``tools/loadtest`` and no unit test imports this file. The behaviour is in ``user.py`` / ``client.py``
(pure, tested against a fake API); this file wires it to ``HttpUser``, the staged ``LoadTestShape`` and the per-process raw log.

    PYTHONPATH=. LOADTEST_RUN_ID=s2-pilot LOADTEST_ORIGIN_AUTH=... locust -f tools/loadtest/locustfile.py \\
        --host http://semigraph-stg.internal:8080 --master          (and --worker --master-host ... on each generator)

Environment (secrets by environment only, never in an argument or a file): ``LOADTEST_RUN_ID`` (required; sent as
``X-Load-Run-Id``), ``LOADTEST_ORIGIN_AUTH`` (``X-Origin-Auth``; required unless the host is loopback),
``LOADTEST_TURNSTILE_TOKEN`` (any non-empty string satisfies the staging stub), ``LOADTEST_OUT_DIR``, ``LOADTEST_WORKER`` (the
salt's worker digit 0-9, one per generator machine; Locust's worker index is the fallback), ``LOADTEST_SALT_START`` (the first
counter value: runs that share an answer cache must not overlap), ``LOADTEST_POOL``, ``LOADTEST_VUS``, ``LOADTEST_SHAPE=off``
(no staged shape: an ad-hoc or smoke run, then ``LOADTEST_PHASE`` labels the records), ``LOADTEST_THINK_MIN_S`` /
``_MAX_S`` / ``LOADTEST_UPLOAD_PERIOD_S`` / ``LOADTEST_UPLOAD_VUS`` (a non-default value is recorded in ``meta`` and makes
the report refuse a gate verdict), ``LOADTEST_READ_TIMEOUT_S``, ``LOADTEST_STREAM_CAP_S``, ``LOADTEST_STATIC_PATHS``,
``LOADTEST_PHASE_WEBHOOK`` (POSTed ``{run_id, phase}`` by the master on every phase change, best effort), ``LOADTEST_SEED``.

The generator refuses to start against anything but loopback or a ``*-stg`` Fly app (``model.check_target_host``).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import random
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import gevent
import locust
from locust import HttpUser, LoadTestShape, events, task
from locust.runners import WorkerRunner

from tools.loadtest import model
from tools.loadtest.client import ClientConfig, LoadClient, StreamGauge
from tools.loadtest.phases import PhaseClock, StreamSampler
from tools.loadtest.pool import DEFAULT_POOL_PATH, load_pool
from tools.loadtest.records import RecordWriter
from tools.loadtest.salt import SALT_FORMAT, Salter, resume_start
from tools.loadtest.user import UploadVirtualUser, UserParams, VirtualUser

log = logging.getLogger("loadtest")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


UPLOAD_VUS = _env_int("LOADTEST_UPLOAD_VUS", model.UPLOAD_VUS)
VUS = _env_int("LOADTEST_VUS", model.VUS)
SHAPE_ON = _env("LOADTEST_SHAPE", "on") != "off"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _params() -> UserParams:
    static = tuple(p.strip() for p in _env("LOADTEST_STATIC_PATHS").split(",") if p.strip())
    return UserParams(think_min_s=_env_float("LOADTEST_THINK_MIN_S", model.THINK_MIN_S),
                      think_max_s=_env_float("LOADTEST_THINK_MAX_S", model.THINK_MAX_S),
                      upload_period_s=_env_float("LOADTEST_UPLOAD_PERIOD_S", model.UPLOAD_PERIOD_S), static_paths=static)


class Runtime:
    """Everything one Locust process shares: the raw log, the salt counter, the pool, the stream gauge and the phase clock."""

    def __init__(self, environment) -> None:
        self.host = model.check_target_host(environment.host)                  # raises on the live app
        self.run_id = _env("LOADTEST_RUN_ID")
        if not self.run_id:
            raise RuntimeError("LOADTEST_RUN_ID is required (it is sent as X-Load-Run-Id and names this run)")
        self.origin_auth = _env("LOADTEST_ORIGIN_AUTH")
        if not self.origin_auth and self.host not in LOOPBACK_HOSTS:
            raise RuntimeError("LOADTEST_ORIGIN_AUTH is required for a non-loopback host (X-Origin-Auth)")
        self.turnstile_token = _env("LOADTEST_TURNSTILE_TOKEN", "loadtest-stub")
        self.worker = self._worker(environment)
        self.params = _params()
        self.pool = load_pool(_env("LOADTEST_POOL") or DEFAULT_POOL_PATH)
        out = Path(_env("LOADTEST_OUT_DIR") or Path(tempfile.gettempdir()) / "loadtest-out") / "generator"      # never the repo
        configured = _env_int("LOADTEST_SALT_START", 0)
        self.salt_start = resume_start(sorted(out.glob(f"events.w{self.worker}.p*.jsonl")), self.worker, self.run_id, configured)
        if self.salt_start > configured:
            log.warning("worker %s restarted: its salt counter resumes at %s, not %s", self.worker, self.salt_start, configured)
        self.salter = Salter(self.worker, self.salt_start)
        self.seed = _env("LOADTEST_SEED", self.run_id)
        self.read_timeout_s = _env_float("LOADTEST_READ_TIMEOUT_S", 60.0)
        self.stream_cap_s = _env_float("LOADTEST_STREAM_CAP_S", 180.0)
        tag = f"w{self.worker}.p{os.getpid()}"
        self.writer = RecordWriter(out / f"events.{tag}.jsonl")
        self.gauge = StreamGauge()
        self.phases = PhaseClock(shape=SHAPE_ON, fixed_phase=_env("LOADTEST_PHASE", "steady"))
        self.sampler = StreamSampler(self.gauge, self.phases, self.writer, run_id=self.run_id, worker=self.worker)
        self._lock = threading.Lock()
        self._vu_seq = 0
        self._upload_seq = 0
        (out / f"meta.{tag}.json").write_text(json.dumps(self.meta(), indent=2) + "\n", encoding="utf-8")
        gevent.spawn(self._sample_loop)

    @staticmethod
    def _worker(environment) -> int:
        if os.environ.get("LOADTEST_WORKER"):
            return int(os.environ["LOADTEST_WORKER"])
        if isinstance(environment.runner, WorkerRunner):
            # Locust's own worker_index is not known until the master answers, and two workers that both fell back to 0 would send
            # the same salted questions: the second use of each is an answer-cache hit.
            raise RuntimeError("LOADTEST_WORKER (the salt's worker digit 0-9, one per generator machine) is required on a Locust worker")
        return 0

    def meta(self) -> dict:
        return {"kind": "meta", "run_id": self.run_id, "worker": self.worker, "pid": os.getpid(), "host": self.host,
                "started_at": time.time(), "params": self.params.as_dict(), "vus": VUS, "upload_vus": UPLOAD_VUS,
                "shape": SHAPE_ON, "fixed_phase": None if SHAPE_ON else self.phases.fixed_phase,
                "salt_format": SALT_FORMAT, "salt_start": self.salt_start,
                "pool_sha256": self.pool.sha256, "target_live_rate": model.TARGET_LIVE_RATE,
                "versions": {"locust": locust.__version__, "python": platform.python_version()}}

    def next_vu(self) -> int:
        with self._lock:
            self._vu_seq += 1
            return self._vu_seq - 1

    def next_upload_slot(self) -> int:
        with self._lock:
            self._upload_seq += 1
            return self._upload_seq - 1

    def _sample_loop(self) -> None:
        while True:
            try:
                self.sampler.tick()
            except Exception:                                                  # noqa: BLE001 - a sampler bug must not stop the run
                log.exception("stream sampler failed")
            gevent.sleep(1.0)

    def log_limits(self, stats: dict) -> None:
        for text in model.limit_warnings(stats, vus=VUS):
            log.warning("staging limits: %s", text)
            self.writer.write({"kind": "warning", "ts": round(time.time(), 3), "run_id": self.run_id, "worker": self.worker,
                               "text": text})


_runtime: Runtime | None = None
_runtime_lock = threading.Lock()


def get_runtime(environment) -> Runtime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = Runtime(environment)
        return _runtime


def _fire_events(environment):
    """A LoadClient listener: the time to first event and the whole ask appear in Locust's own console / UI too."""
    def listener(record: dict) -> None:
        if record.get("kind") != "ask":
            return
        try:
            failure = None if record["outcome"] == "done" else Exception(record["outcome"])
            if record.get("ttfe_s") is not None:
                environment.events.request.fire(request_type="SSE", name=f"ttfe[{record['klass']}]", response_length=0,
                                                response_time=record["ttfe_s"] * 1000, exception=None, context={})
            environment.events.request.fire(request_type="SSE", name=f"ask_complete[{record['klass']}]",
                                            response_length=record.get("bytes") or 0, response_time=record["total_s"] * 1000,
                                            exception=failure, context={})
        except Exception:                                                      # noqa: BLE001 - metrics must never break an ask
            log.exception("could not fire the locust event for an ask")
    return listener


def _make_client(user: HttpUser, rt: Runtime, vu: int, ip: str) -> LoadClient:
    config = ClientConfig(run_id=rt.run_id, worker=rt.worker, vu=vu, ip=ip, origin_auth=rt.origin_auth,
                          turnstile_token=rt.turnstile_token, read_timeout_s=rt.read_timeout_s, stream_cap_s=rt.stream_cap_s)

    def send(method, path, label=None, **kwargs):
        return user.client.request(method, path, name=label, **kwargs)

    return LoadClient(send, config, rt.writer, gauge=rt.gauge, phase=rt.phases.phase, listeners=(_fire_events(user.environment),))


class MainUser(HttpUser):
    """One of the 1,000 visitors: a pre-registered iteration every 120-300 s (start to start)."""

    def wait_time(self) -> float:
        return self.vu.wait_time()

    def on_start(self) -> None:
        rt = get_runtime(self.environment)
        seq = rt.next_vu()
        self.rt = rt
        rng = random.Random(f"{rt.seed}:{rt.worker}:{seq}")
        client = _make_client(self, rt, seq, model.vu_ip(rt.run_id, rt.worker, seq))
        self.vu = VirtualUser(client, rt.pool, rt.salter, rng, rt.params, on_stats=rt.log_limits)
        rt.phases.start()
        gevent.sleep(self.vu.start_delay())

    @task
    def iteration(self) -> None:
        try:
            self.vu.run_iteration()
        except Exception as e:                                                 # noqa: BLE001 - one bad iteration is recorded
            log.exception("iteration failed")
            self.vu.client.emit("iteration_error", detail=f"{type(e).__name__}: {str(e)[:200]}")


class UploadUser(HttpUser):
    """The 5-VU upload population: one upload per period, watched to ``ready``."""

    fixed_count = UPLOAD_VUS
    abstract = UPLOAD_VUS <= 0

    def wait_time(self) -> float:
        return self.uploader.wait_time()

    def on_start(self) -> None:
        rt = get_runtime(self.environment)
        slot = rt.next_upload_slot()
        vu = model.UPLOAD_SEQ_BASE + slot
        rng = random.Random(f"{rt.seed}:{rt.worker}:upload:{slot}")
        client = _make_client(self, rt, vu, model.vu_ip(rt.run_id, rt.worker, slot, upload=True))
        # three uploaders on worker 0, two on worker 1: slots 0..2 and 3..4 of the 5 spread over the period
        self.uploader = UploadVirtualUser(client, rt.params, rng, stagger_slot=rt.worker * 3 + slot, slots=max(UPLOAD_VUS, 1))
        rt.phases.start()
        gevent.sleep(self.uploader.start_delay())

    @task
    def cycle(self) -> None:
        try:
            self.uploader.run_cycle()
        except Exception as e:                                                 # noqa: BLE001
            log.exception("upload cycle failed")
            self.uploader.client.emit("iteration_error", detail=f"{type(e).__name__}: {str(e)[:200]}")


@events.init.add_listener
def _on_init(environment, **_kwargs) -> None:
    """Fail fast, on every node, if the target is not a staging host."""
    if environment.host:
        model.check_target_host(environment.host)
    if not _env("LOADTEST_RUN_ID"):
        raise RuntimeError("LOADTEST_RUN_ID is required")


@events.quitting.add_listener
def _on_quit(environment, **_kwargs) -> None:
    if _runtime is not None:
        _runtime.writer.close()


def _post_phase(url: str, run_id: str, phase: str) -> None:
    try:
        request = urllib.request.Request(url, data=json.dumps({"run_id": run_id, "phase": phase}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(request, timeout=5).close()                      # noqa: S310 - an operator-supplied staging URL
    except Exception as e:                                                      # noqa: BLE001
        log.warning("phase webhook failed: %s", type(e).__name__)


if SHAPE_ON:

    class StagedShape(LoadTestShape):
        """ramp 10 min -> steady 20 -> spike +500 for 3 -> soak 60 -> fault 10 (model.PHASES); runs on the master only."""

        _phase: str | None = None

        def tick(self):
            t = self.get_run_time()
            target = model.target_users(t, VUS)
            if target is None:
                return None
            phase = model.phase_at(t)
            if phase != self._phase:
                self._phase = phase
                url = _env("LOADTEST_PHASE_WEBHOOK")
                if url:
                    threading.Thread(target=_post_phase, args=(url, _env("LOADTEST_RUN_ID"), phase), daemon=True).start()
            users, rate = target
            return users + max(UPLOAD_VUS, 0), rate

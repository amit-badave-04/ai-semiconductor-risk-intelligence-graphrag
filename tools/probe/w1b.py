"""The driver of window W1b: ONE fresh boot of the real service under M4's max-size upload, and the verdict of the three boots.

Council 7, decision 1, step 3 (docs/v2/research/m5-councils/council7/verdict.md; ``deploy/staging/windows.json`` "W1b"). Run from
the repository root, on the developer machine, against the staging API of the window (``semigraph-stg``, the fp32 image on a fresh
shared-cpu-2x / 4 GB machine, the staging database, the mock LLM):

    python -m tools.probe.w1b boot --boot 1 --wait-for-machine-s 2700 --out boot-1.json    # started BEFORE the deploy
    python -m tools.probe.w1b aggregate boot-1.json boot-2.json boot-3.json --out w1b.json

``boot`` does, for one freshly created machine: finds the machine (exactly one that is not destroyed, created after this run
started when it waited for it) and its creation time in ``flyctl machine list``; polls ``/healthz`` until 200 (creation to
healthy); waits until the kill level reads ``off``; starts ``tools/probe/memsample.py`` ON the machine (shipped inside a
``python -c`` command over ``flyctl ssh console``) and checks that it sees the serving process; creates a workspace and uploads
the 30-page max-size PDF exactly as the page does; WHILE the job embeds, sends salted mock-LLM asks, at most the live in-flight
cap (2) at a time, each from its own client address, until the job is ready plus 30 s; stops and reads the sampler; reads the
machine's events and logs for an OOM kill or a restart; prints ONE JSON object.

The pre-registered rule and the verdicts (FAIL / INVALID / INCONCLUSIVE / PASS, fail closed) are in ``tools/probe/w1b_judge.py``.

Secrets: the origin-header secret and the admin token are read from the state directory ``scripts/staging.py`` writes (outside
the repository), used in request headers only, and scrubbed, with the workspace id and token, from everything printed or written.
Every flyctl call is an argument list that must name the registered staging API app; the target URL must be that app or loopback.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import hashlib
import importlib.util
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import zlib
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.probe import memsample  # noqa: E402
from tools.probe.w1b_judge import (  # noqa: E402,F401  (re-exported: the tests and the aggregate read them from here)
    COVERAGE_SLACK_S,
    EXPECTED_EMBED_SLOTS,
    EXPECTED_GUEST,
    HEALTHY_LIMIT_S,
    VMHWM_LIMIT_KB,
    WINDOW,
    Scrub,
    aggregate,
    analyze_events,
    analyze_logs,
    boot_verdict,
    judge_oom_or_restart,
    parse_sampler_lines,
    summarize_samples,
)

WINDOWS_JSON = ROOT / "deploy" / "staging" / "windows.json"
FIXTURES = ROOT / "tests" / "upload_fixtures.py"
BENCHMARK = ROOT / "artifacts" / "benchmark.json"
LIVE_APPS = frozenset({"semigraph", "semigraph-neo4j"})
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})

# The live cap the load is shaped by: fly.toml sets none, so it is the code default (a test pins both).
LIVE_MAX_CONCURRENT_ANSWERS = 2
PDF_PAGES, PDF_CHARS_PER_PAGE = 30, 2700
TAIL_S = 30.0
JOB_TIMEOUT_S = 90 + 1200 + 120 + 60          # parse + embed + compare budgets of the upload job (config.py) and a margin
HEALTHY_WAIT_S = 480.0                         # how long after creation /healthz is polled before the boot counts as never healthy
MACHINE_AGE_SLACK_S = 120.0                    # with --wait-for-machine-s, a machine older than this at start is the old one
GATE_WAIT_S = 90.0                             # the kill level reads "on" until the maintenance thread first reads it
RETRY_503_S = 60.0
ASK_CAP_S = 180.0
STUB_TURNSTILE = "w1b-stub"
SAMPLER_OUT, SAMPLER_STOP, SAMPLER_MAX_S = "/tmp/memsample.jsonl", "/tmp/memsample.stop", 1800
BOOTSTRAP = "import base64,sys,zlib;exec(zlib.decompress(base64.b64decode(sys.argv.pop(1))))"


# tools/loadtest is not in the tools image (tests/test_tools_s7.py: every tools module the probe imports is copied there), so the two
# small pieces of it this driver needs are written here and pinned to the originals by tests/test_tools_probe_w1b.py: the per-ask
# salt (tools/loadtest/salt.py) and the event-stream framing (tools/loadtest/sse.py).
SALT_FORMAT = "{q} (ref {worker}{n:06d})"
MAX_SALT_COUNTER = 999_999
MAX_BASE_CHARS = 500 - len(" (ref 0000000)")        # Settings.max_question_chars less the salt: the longest question that can take it


def apply_salt(question: str, worker: int, n: int) -> str:
    """``question (ref <worker digit><six-digit counter>)``: one run of seven digits, so the year detectors of the retrieval never see
    it, and a new answer-cache key and embedding input for every ask."""
    if type(worker) is not int or not 0 <= worker <= 9 or type(n) is not int or not 0 <= n <= MAX_SALT_COUNTER:
        raise ValueError(f"the salt needs a worker digit 0-9 and a counter 0-{MAX_SALT_COUNTER}, got {worker!r}, {n!r}")
    return SALT_FORMAT.format(q=question, worker=worker, n=n)


def normalize_question(question: str) -> str:
    """The text the answer cache's key is made of: lower case, white space collapsed, trailing ``?.! `` stripped."""
    return " ".join(question.lower().split()).rstrip("?.! ")


class SseReader:
    """The event-stream framing the site speaks, as bytes arrive: ``feed(chunk)`` returns the ``(event, data)`` of each block that a
    blank line completed (``\\n``, ``\\r\\n`` or ``\\r`` line ends, ``:`` comment lines skipped, several ``data:`` lines joined, a
    multibyte character split between two chunks kept whole). A block without data is dropped; bytes that are not UTF-8 raise
    ``UnicodeDecodeError``."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[tuple[str | None, str]]:
        buffer = self._buffer + self._decoder.decode(chunk)
        held = "\r" if buffer.endswith("\r") else ""             # a lone CR may be the first half of a CRLF: wait for the next chunk
        text = buffer[:len(buffer) - len(held)].replace("\r\n", "\n").replace("\r", "\n")
        *blocks, rest = text.split("\n\n")
        self._buffer = rest + held                                # the unfinished block stays for the next chunk
        events = []
        for block in blocks:
            name, data = None, []
            for line in block.split("\n"):
                field, _, value = line.partition(":")
                if not line.startswith(":"):                     # ``:`` opens a comment (the keep-alive ping)
                    value = value[1:] if value.startswith(" ") else value
                    if field == "event":
                        name = value
                    elif field == "data":
                        data.append(value)
            if data:
                events.append((name, "\n".join(data)))
        return events


class W1bError(Exception):
    """A problem before or around the measurement; the message never holds a secret."""


class MachineCountError(W1bError):
    """The API app does not have exactly one machine that is not destroyed (none yet, or the old one is still there)."""


class Result(NamedTuple):
    returncode: int
    stdout: bytes
    stderr: bytes


# ---- registry, secrets, target ------------------------------------------------------------------------------------------------

def load_registry(windows_path: Path = WINDOWS_JSON) -> dict:
    doc = json.loads(Path(windows_path).read_text(encoding="utf-8"))
    name = doc["apps"]["api"]["name"]
    if name in LIVE_APPS or "api" not in doc["windows"][WINDOW]["apps"]:
        raise W1bError("windows.json does not register a staging API for window W1b")
    return {"api_app": name}


def load_secrets(state_dir: Path | None, scrub: Scrub) -> tuple[str, str]:
    """``(origin-header secret, admin token)`` of the window's API from the state file ``scripts/staging.py create`` wrote."""
    directory = Path(state_dir or os.environ.get("STAGING_STATE_DIR") or Path.home() / ".semigraph-staging")
    path = directory / f"{WINDOW}.json"
    try:
        api = json.loads(path.read_text(encoding="utf-8"))["secrets"]["api"]
        origin, admin = api["ORIGIN_AUTH_SECRET"], api["ADMIN_TOKEN"]
    except (OSError, ValueError, KeyError, TypeError):
        raise W1bError(f"the window's state file {path} is missing or has no API secrets: run `staging.py create {WINDOW}` first") from None
    scrub.add(origin, admin)
    return origin, admin


def check_target(base_url: str, api_app: str) -> str:
    """The base URL if it is the registered staging API or loopback (a test double); anything else, the live app first, is refused."""
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if parts.username or parts.password or parts.scheme not in ("http", "https"):
        raise W1bError("the target URL must be plain http(s) without user information")
    if host in LOOPBACK or (parts.scheme == "https" and host == f"{api_app}.fly.dev"):
        return base_url.rstrip("/")
    raise W1bError(f"the target must be https://{api_app}.fly.dev (the window's staging API) or loopback")


def run_subprocess(args: list[str], timeout: float) -> Result:
    done = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
    return Result(done.returncode, done.stdout or b"", done.stderr or b"")


class Fly:
    """Every flyctl call of the driver: an argument list that names exactly the registered staging API app."""

    def __init__(self, app: str, runner: Callable[[list[str], float], Result] = run_subprocess, flyctl: str = "flyctl"):
        self.app, self.runner, self.flyctl = app, runner, flyctl

    def __call__(self, args: Sequence[str], timeout: float = 120) -> Result:
        args = list(args)
        named = [args[i + 1] for i, a in enumerate(args) if a in ("-a", "--app") and i + 1 < len(args)]
        if named != [self.app]:
            raise W1bError("refusing a flyctl call that does not name exactly the registered staging API app")
        try:
            return self.runner([self.flyctl, *args], timeout)
        except subprocess.TimeoutExpired:
            return Result(124, b"", f"flyctl {' '.join(args[:2])} timed out".encode())
        except OSError:
            raise W1bError("flyctl could not be started: is it installed and on the PATH?") from None


def _json(result: Result) -> Any:
    try:
        return json.loads(result.stdout.decode("utf-8", "replace") or "null") if result.returncode == 0 else None
    except ValueError:
        return None


def parse_time(text: str) -> float:
    """Epoch seconds of an ISO-8601 time (Fly writes up to nine fractional digits)."""
    cleaned = re.sub(r"(\.\d{6})\d+", r"\1", text.strip().replace("Z", "+00:00"))
    stamp = datetime.fromisoformat(cleaned)
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp()


def current_machine(fly: Fly, machine_id: str | None = None) -> dict:
    """The ONE machine of the API app that is not destroyed (a second one could take the requests), with its creation time and
    guest. The class is not asserted here: ``boot_verdict`` judges it."""
    rows = _json(fly(["machine", "list", "-a", fly.app, "--json"]))
    if not isinstance(rows, list):
        raise W1bError("could not read `flyctl machine list` of the API app")
    live = [m for m in rows if isinstance(m, dict) and m.get("state") != "destroyed"]
    if len(live) != 1:
        raise MachineCountError(f"expected exactly one machine in {fly.app} (destroy the old one first), found {len(live)}")
    machine = live[0]
    if machine_id and machine.get("id") != machine_id:
        raise MachineCountError("the machine of the API app is not the one named by --machine-id")
    guest = (machine.get("config") or {}).get("guest") or {}
    return {"id": machine.get("id"), "created_at": machine.get("created_at"), "state": machine.get("state"),
            "region": machine.get("region"), "guest": {key: guest.get(key) for key in EXPECTED_GUEST}}


def wait_for_machine(fly: Fly, machine_id: str | None, *, wait_s: float, not_before: float, now: Callable[[], float],
                     sleep: Callable[[float], None], poll_s: float = 3.0) -> dict:
    """The one machine of the app, waiting up to ``wait_s`` for it to exist (the driver may be started BEFORE the deploy that
    creates the machine, so that /healthz is polled from the moment it is created and not from when the deploy returns). While
    waiting, a machine created before ``not_before`` is the old one that is still there, never the one to measure. With
    ``wait_s`` 0 the machine must exist now."""
    deadline = now() + wait_s
    while True:
        try:
            machine = current_machine(fly, machine_id)
            if wait_s <= 0 or (machine["created_at"] and parse_time(machine["created_at"]) >= not_before):
                return machine
            problem = "the machine of the API app was created before this run started (the old one is still there)"
        except MachineCountError as exc:
            problem = str(exc)
        if now() >= deadline:
            raise MachineCountError(problem)
        sleep(poll_s)


# ---- the sampler on the machine ---------------------------------------------------------------------------------------------------

def memsample_source() -> str:
    return Path(memsample.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")


def sampler_command(source: str, *, duration_s: int = SAMPLER_MAX_S) -> str:
    """The remote command: the sampler's text, compressed and base64'd, run by ``exec`` inside ``python -c`` (the one word
    ``sys.argv.pop(1)`` is the payload; the rest are the sampler's own options). No quote inside the program, no space in the payload."""
    blob = base64.b64encode(zlib.compress(source.encode("utf-8"), 9)).decode("ascii")
    return (f"python -c {shlex.quote(BOOTSTRAP)} {blob} --daemon --out {SAMPLER_OUT} --stop-file {SAMPLER_STOP} "
            f"--duration-s {int(duration_s)}")


def sampler_duration_s(job_timeout_s: float, tail_s: float) -> int:
    """How long the sampler may run by itself: the longest workload (the job's timeout, the tail, the last ask to finish, the
    start checks and the kill-level wait) and a margin, so a slow but valid job is never cut off by the sampler's own limit."""
    return int(job_timeout_s + tail_s + ASK_CAP_S + GATE_WAIT_S + 300)


def start_sampler(fly: Fly, *, sleep: Callable[[float], None], log: Callable[[str], None], duration_s: int = SAMPLER_MAX_S) -> None:
    result = fly(["ssh", "console", "-a", fly.app, "-C", sampler_command(memsample_source(), duration_s=duration_s)], timeout=120)
    if result.returncode != 0:
        raise W1bError("the sampler could not be started over `flyctl ssh console`")
    for _ in range(3):
        sleep(3)
        look = fly(["ssh", "console", "-a", fly.app, "-C", f"tail -n 5 {SAMPLER_OUT}"], timeout=60)
        _, samples = parse_sampler_lines(look.stdout.decode("utf-8", "replace"))
        if any(s.get("uvicorn_pid") for s in samples):
            log(f"sampler running, serving process pid {[s['uvicorn_pid'] for s in samples if s.get('uvicorn_pid')][-1]}")
            return
    raise W1bError("the sampler is not running or does not see the serving process (python -m semigraph.serve.drain)")


def stop_and_collect_sampler(fly: Fly, *, sleep: Callable[[float], None]) -> tuple[dict, list[dict]] | None:
    fly(["ssh", "console", "-a", fly.app, "-C", f"touch {SAMPLER_STOP}"], timeout=60)
    sleep(3)
    result = fly(["ssh", "console", "-a", fly.app, "-C", f"cat {SAMPLER_OUT}"], timeout=180)
    return parse_sampler_lines(result.stdout.decode("utf-8", "replace")) if result.returncode == 0 else None


def collect_machine_evidence(fly: Fly, machine_id: str, scrub: Scrub) -> dict:
    """Machine events and logs of the measured machine. Each source is ``None`` when it could not be read."""
    status = analyze_events(_json(fly(["machine", "status", machine_id, "-a", fly.app, "--json"])))
    by_machine = fly(["logs", "-a", fly.app, "--machine", machine_id, "--no-tail"], timeout=180)
    if by_machine.returncode != 0:                                   # the flag may be unknown to this flyctl: filter the app's lines
        by_machine = fly(["logs", "-a", fly.app, "--no-tail"], timeout=180)
    logs = analyze_logs(by_machine.stdout.decode("utf-8", "replace"), machine_id, scrub) if by_machine.returncode == 0 else None
    return {"events": status, "logs": logs}


# ---- the workload -------------------------------------------------------------------------------------------------------------------

class Context:
    """Everything one boot's HTTP traffic needs: the client, the target, the clock, the secrets (held only as header values)."""

    def __init__(self, client: httpx.Client, base: str, origin: str, admin: str, scrub: Scrub, *, boot: int,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda line: None):
        self.client, self.base, self.origin, self.admin, self.scrub = client, base, origin, admin, scrub
        self.boot, self.now, self.sleep, self.log = boot, now, sleep, log

    def headers(self, ip: str, **extra: str) -> dict[str, str]:
        return {"X-Origin-Auth": self.origin, "X-Test-Client-IP": ip, **extra}

    def request(self, method: str, path: str, ip: str, **kwargs: Any) -> httpx.Response | None:
        """One request; None on a transport error (the caller decides what a missing answer means)."""
        extra = kwargs.pop("extra_headers", {})
        try:
            return self.client.request(method, self.base + path, headers=self.headers(ip, **extra), **kwargs)
        except httpx.HTTPError:
            return None

    def retrying(self, method: str, path: str, ip: str, ok: tuple[int, ...], **kwargs: Any) -> tuple[httpx.Response | None, int]:
        """``request`` again with a growing pause while the service answers 503 (its kill level reads "on" until first read, or it
        is draining), for at most ``RETRY_503_S``. Returns the last response and the number of retries."""
        started, delay, retries = self.now(), 1.0, 0
        while True:
            response = self.request(method, path, ip, **kwargs)
            if response is not None and (response.status_code in ok or response.status_code != 503):
                return response, retries
            if self.now() - started >= RETRY_503_S:
                return response, retries
            retries += 1
            self.sleep(delay)
            delay = min(delay * 2, 10.0)


def wait_healthy(ctx: Context, boot_started_at: float, *, poll_s: float = 2.0, max_wait_s: float = HEALTHY_WAIT_S) -> dict:
    first_after = ctx.now() - boot_started_at
    polls = 0
    while True:
        polls += 1
        response = ctx.request("GET", "/healthz", "10.0.0.1", timeout=5.0)
        elapsed = ctx.now() - boot_started_at
        if response is not None and response.status_code == 200:
            try:
                embedder = response.json().get("embedder")
            except ValueError:
                embedder = None
            return {"boot_to_healthy_s": round(elapsed, 1), "healthy_on_first_poll": polls == 1, "polls": polls,
                    "first_poll_after_creation_s": round(first_after, 1), "embedder": embedder}
        if elapsed >= max_wait_s:
            return {"boot_to_healthy_s": None, "healthy_on_first_poll": False, "polls": polls,
                    "first_poll_after_creation_s": round(first_after, 1), "embedder": None}
        ctx.sleep(poll_s)


def wait_kill_level_off(ctx: Context, *, poll_s: float = 2.0, max_wait_s: float = GATE_WAIT_S) -> str:
    """The effective kill level through the admin route; ``off`` once the maintenance thread has read it (until then the gates read
    ``on`` and every ask and upload is a 503). Returns the last level seen."""
    started, level = ctx.now(), "unreadable"
    while True:
        response = ctx.request("GET", "/api/admin/policy", "10.0.0.1", extra_headers={"X-Admin-Token": ctx.admin}, timeout=10.0)
        if response is not None and response.status_code == 200:
            try:
                level = str(response.json().get("effective"))
            except ValueError:
                level = "unreadable"
            if level == "off":
                return level
        if ctx.now() - started >= max_wait_s:
            return level
        ctx.sleep(poll_s)


def load_pdf(path: Path | None = None) -> bytes:
    """The 30-page, 2,700-characters-per-page PDF of M4's gate G4: the test fixture's own builder, loaded by path (it is stdlib at
    module level), or ``--pdf``."""
    if path is not None:
        return Path(path).read_bytes()
    spec = importlib.util.spec_from_file_location("w1b_upload_fixtures", FIXTURES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.max_size_pdf(n_pages=PDF_PAGES, chars_per_page=PDF_CHARS_PER_PAGE)


def load_questions(path: Path = BENCHMARK) -> list[str]:
    """The benchmark's questions, de-duplicated, short enough to take the per-ask salt. They are the seeded cached examples when sent
    as they are, so every ask is salted (a verbatim ask is a cache hit: no embedding, no model call)."""
    seen, out = set(), []
    for row in json.loads(Path(path).read_text(encoding="utf-8")):
        text = " ".join(str(row.get("q", "")).split())
        if text and len(text) <= MAX_BASE_CHARS and normalize_question(text) not in seen:
            seen.add(normalize_question(text))
            out.append(text)
    if not out:
        raise W1bError("no usable question in artifacts/benchmark.json")
    return out


def ask_ip(boot: int, n: int) -> str:
    return f"10.{boot}.{(n >> 8) & 0xFF}.{n & 0xFF}"


def ask_once(ctx: Context, question: str, ip: str, cap_s: float = ASK_CAP_S) -> str:
    """One streamed ask; returns its outcome: done | done_cached | error_event | http_<status> | dropped | protocol_error | timeout |
    exception. It reads to the end of the stream, as the page does."""
    body = {"question": question, "strategy": "hybrid", "turnstile_token": STUB_TURNSTILE}
    started, outcome, reader = ctx.now(), None, SseReader()
    try:
        with ctx.client.stream("POST", f"{ctx.base}/api/ask", json=body, headers=ctx.headers(ip)) as response:
            if response.status_code != 200:
                return f"http_{response.status_code}"
            for chunk in response.iter_bytes():
                for name, data in reader.feed(chunk):
                    if name in ("done", "error"):
                        payload = json.loads(data)
                        if not isinstance(payload, dict) or payload.get("event") != name:
                            return "protocol_error"
                        outcome = "error_event" if name == "error" else ("done_cached" if payload.get("cached") is True else "done")
                if ctx.now() - started > cap_s:
                    return "timeout"
    except ValueError:                                           # not UTF-8, or an event that is not JSON
        return "protocol_error"
    except httpx.TimeoutException:
        return "timeout"
    except httpx.HTTPError:
        return "exception"
    return outcome or "dropped"


class AskLoop:
    """``concurrency`` threads, each asking one salted question after another until ``stop`` is set. The records (start, end,
    outcome) are kept so the verdict can tell which asks overlapped the job."""

    def __init__(self, ctx: Context, questions: list[str], concurrency: int, salt_start: int):
        self.ctx, self.questions, self.concurrency, self.salt_start = ctx, questions, concurrency, salt_start
        self.stop, self.records, self._lock, self._count = threading.Event(), [], threading.Lock(), itertools.count()
        self._threads: list[threading.Thread] = []

    def _next(self) -> int:
        with self._lock:
            return next(self._count)

    def _work(self) -> None:
        while not self.stop.is_set():
            n = self._next()
            question = apply_salt(self.questions[n % len(self.questions)], self.ctx.boot, self.salt_start + n)
            started = self.ctx.now()
            outcome = ask_once(self.ctx, question, ask_ip(self.ctx.boot, n))
            with self._lock:
                self.records.append({"start": started, "end": self.ctx.now(), "outcome": outcome})
            if outcome not in ("done", "done_cached"):
                self.ctx.sleep(0.5)                       # a refusal or a drop: do not hammer

    def start(self) -> None:
        self._threads = [threading.Thread(target=self._work, name=f"w1b-ask-{i}", daemon=True) for i in range(self.concurrency)]
        for thread in self._threads:
            thread.start()

    def finish(self, join_s: float = ASK_CAP_S + 10) -> None:
        self.stop.set()
        for thread in self._threads:
            thread.join(join_s)

    def summary(self, job_start: float, job_end: float) -> dict:
        with self._lock:
            records = list(self.records)
        outcomes = Counter(r["outcome"] for r in records)
        overlap = sum(1 for r in records if r["outcome"] == "done" and r["start"] < job_end and r["end"] > job_start)
        return {"sent": len(records), "done": outcomes["done"], "cached": outcomes["done_cached"], "done_overlapping_job": overlap,
                "errors": {k: v for k, v in sorted(outcomes.items()) if k not in ("done", "done_cached")}}


def watch_job(ctx: Context, ws: str, token: str, job_id: str, ip: str, *, deadline: float) -> dict:
    """The job's progress stream until a terminal state, reconnecting when it drops (the page does the same; at most three watchers
    per job are allowed, one is used). ``state`` is ``ready``, ``failed``, ``gone`` (404) or ``timeout``."""
    last: dict = {"state": "queued"}
    progress: dict = {}
    headers = ctx.headers(ip, **{"X-Workspace-Token": token})
    while ctx.now() < deadline:
        try:
            with ctx.client.stream("GET", f"{ctx.base}/api/workspace/{ws}/jobs/{job_id}", headers=headers,
                                   timeout=httpx.Timeout(30.0, connect=10.0)) as response:
                if response.status_code == 404:
                    return {"state": "gone", "chunks_total": progress.get("total")}
                if response.status_code == 200:
                    reader = SseReader()
                    for chunk in response.iter_bytes():
                        for name, data in reader.feed(chunk):
                            payload = json.loads(data) if name == "job" else None
                            if isinstance(payload, dict) and isinstance(payload.get("state"), str):
                                last = {**last, **payload}
                                progress = payload.get("progress") or progress
                                if payload["state"] in ("ready", "failed"):
                                    return {"state": payload["state"], "chunks_total": progress.get("total"), "error": last.get("error")}
        except (httpx.HTTPError, ValueError):                    # a dropped stream, or bytes that are not an event stream: watch again
            pass
        ctx.sleep(2.0)
    return {"state": "timeout", "chunks_total": progress.get("total"), "last_state": last.get("state")}


def _not_started(why: str, retries: int) -> dict:
    return {"job": {"state": "not_started", "error": why}, "asks": {}, "workload_s": 0.0, "retries": retries}


def run_workload(ctx: Context, pdf: bytes, questions: list[str], *, concurrency: int, tail_s: float, job_timeout_s: float,
                 salt_start: int) -> dict:
    """Create a workspace, upload the PDF, ask while the job embeds, and keep asking for ``tail_s`` after it is ready. The workspace
    id and token live in this function only."""
    ws_ip = f"172.16.{ctx.boot}.{int(ctx.now()) % 250 + 1}"
    started = ctx.now()
    created, retries = ctx.retrying("POST", "/api/workspace", ws_ip, ok=(201,), json={"turnstile_token": STUB_TURNSTILE}, timeout=30.0)
    if created is None or created.status_code != 201:
        return _not_started(f"workspace create answered {getattr(created, 'status_code', None)}", retries)
    info = created.json()
    ws, token = info["workspace_id"], info["token"]
    ctx.scrub.add(ws, token)
    try:
        uploaded, upload_retries = ctx.retrying("POST", f"/api/workspace/{ws}/documents", ws_ip, ok=(202,),
                                                files={"file": ("w1b-max-size.pdf", pdf, "application/pdf")},
                                                data={"title": "W1b max-size upload"}, timeout=httpx.Timeout(120.0, connect=10.0),
                                                extra_headers={"X-Workspace-Token": token, "X-Turnstile-Token": STUB_TURNSTILE})
        retries += upload_retries
        if uploaded is None or uploaded.status_code != 202:
            return _not_started(f"upload answered {getattr(uploaded, 'status_code', None)}", retries)
        accepted = ctx.now()
        loop = AskLoop(ctx, questions, concurrency, salt_start)
        loop.start()
        try:
            result = watch_job(ctx, ws, token, uploaded.json()["job_id"], ws_ip, deadline=accepted + job_timeout_s)
            ready_at = ctx.now()
            ctx.sleep(tail_s)
        finally:
            loop.finish()
        job = {**result, "ready_s": round(ready_at - accepted, 1) if result["state"] == "ready" else None}
        return {"job": job, "asks": loop.summary(accepted, ready_at), "workload_s": round(ctx.now() - started, 1), "retries": retries}
    finally:
        ctx.request("DELETE", f"/api/workspace/{ws}", ws_ip, extra_headers={"X-Workspace-Token": token}, timeout=30.0)


def read_settings_evidence(ctx: Context) -> dict:
    """The deployed embedder slots, from the admin state's limiters (advisory unless it disagrees with the live value)."""
    response = ctx.request("GET", "/api/admin/state", "10.0.0.1", extra_headers={"X-Admin-Token": ctx.admin}, timeout=15.0)
    try:
        limiters = response.json().get("limiters", {}) if response is not None and response.status_code == 200 else {}
    except ValueError:
        limiters = {}
    return {"embed_slots": (limiters.get("embed") or {}).get("total")}


# ---- one boot --------------------------------------------------------------------------------------------------------------------------

def run_boot(args: argparse.Namespace, ctx: Context, fly: Fly, *, pdf: bytes, questions: list[str]) -> dict:
    machine = wait_for_machine(fly, args.machine_id, wait_s=args.wait_for_machine_s, not_before=ctx.now() - MACHINE_AGE_SLACK_S,
                               now=ctx.now, sleep=ctx.sleep)
    started_at = parse_time(args.boot_started_at if args.boot_started_at != "auto" else machine["created_at"] or "")
    report: dict = {"window": WINDOW, "boot": args.boot, "machine": machine,
                    "boot_started_at": datetime.fromtimestamp(started_at, UTC).isoformat(timespec="seconds"),
                    "pdf": {"sha256": hashlib.sha256(pdf).hexdigest(), "bytes": len(pdf), "pages": PDF_PAGES},
                    "limits": {"vmhwm_kb": VMHWM_LIMIT_KB, "healthy_s": HEALTHY_LIMIT_S, "ask_concurrency": args.concurrency,
                               "tail_s": args.tail_s}}
    report["healthz"] = wait_healthy(ctx, started_at)
    sampler_started, summary = False, None
    if report["healthz"]["boot_to_healthy_s"] is not None:
        report["kill_level"] = wait_kill_level_off(ctx)
        try:
            sampler_started = True                    # even a start that failed half way gets a stop and a read below
            start_sampler(fly, sleep=ctx.sleep, log=ctx.log, duration_s=sampler_duration_s(args.job_timeout_s, args.tail_s))
            workload = run_workload(ctx, pdf, questions, concurrency=args.concurrency, tail_s=args.tail_s,
                                    job_timeout_s=args.job_timeout_s, salt_start=args.salt_start)
        except Exception as exc:  # noqa: BLE001  a boot that cannot be measured is reported (INVALID), never lost to a traceback
            workload = _not_started(ctx.scrub(f"{type(exc).__name__}: {exc}"), 0)
        report.update(workload)
        report["settings_evidence"] = read_settings_evidence(ctx)
    if sampler_started:
        collected = stop_and_collect_sampler(fly, sleep=ctx.sleep)
        summary = summarize_samples(*collected) if collected else None
    report["memory"] = summary or {}
    report["oom_or_restart"] = judge_oom_or_restart(summary, collect_machine_evidence(fly, machine["id"], ctx.scrub), ctx.scrub)
    for key in ("max_vmhwm_kb", "min_mem_available_kb", "max_swap_used_kb"):
        report[key] = report["memory"].get(key)
    report["boot_to_healthy_s"], report["job_ready_s"] = report["healthz"]["boot_to_healthy_s"], (report.get("job") or {}).get("ready_s")
    report["verdict"], report["reasons"], report["criteria"] = boot_verdict(report)
    report["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="w1b", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("boot", help="measure ONE freshly created machine and print its JSON")
    boot.add_argument("--boot", type=int, required=True, choices=range(1, 10), help="1, 2, 3: also the salt's worker digit")
    boot.add_argument("--base-url", default=None, help="default: the window's staging API; only that or loopback is accepted")
    boot.add_argument("--machine-id", default=None)
    boot.add_argument("--wait-for-machine-s", type=float, default=0.0,
                      help="start BEFORE the deploy and wait this long for the new machine to appear (0: it must exist now)")
    boot.add_argument("--boot-started-at", default="auto", help="ISO time of the machine's creation; auto = created_at from flyctl")
    boot.add_argument("--state-dir", type=Path, default=None, help="where scripts/staging.py keeps the window's secrets")
    boot.add_argument("--concurrency", type=int, default=LIVE_MAX_CONCURRENT_ANSWERS, choices=range(1, LIVE_MAX_CONCURRENT_ANSWERS + 1),
                      help="asks in flight at once: at most the live cap")
    boot.add_argument("--tail-s", type=float, default=TAIL_S, help="keep asking this long after the job is ready")
    boot.add_argument("--job-timeout-s", type=float, default=JOB_TIMEOUT_S)
    boot.add_argument("--salt-start", type=int, default=None, help="first ask counter (default: from the clock, so a re-run never repeats a salt)")
    boot.add_argument("--pdf", type=Path, default=None, help="use this file instead of the max-size fixture")
    boot.add_argument("--windows", type=Path, default=WINDOWS_JSON)
    boot.add_argument("--flyctl", default="flyctl")
    boot.add_argument("--out", type=Path, default=None)
    agg = sub.add_parser("aggregate", help="the W1b verdict of three boot files")
    agg.add_argument("files", nargs="+", type=Path)
    agg.add_argument("--out", type=Path, default=None)
    return parser


def emit(report: dict, out: Path | None, scrub: Scrub) -> None:
    text = scrub(json.dumps(report, indent=1, default=str))
    if out is not None:
        Path(out).write_text(text + "\n", encoding="utf-8")
    print(text)


def exit_code(verdict: str) -> int:
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


def main(argv: Sequence[str] | None = None, *, runner: Callable[[list[str], float], Result] = run_subprocess,
         client: httpx.Client | None = None, now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> int:
    args = build_parser().parse_args(argv)
    scrub = Scrub()
    try:
        if args.command == "aggregate":
            result = aggregate([json.loads(Path(p).read_text(encoding="utf-8")) for p in args.files])
            emit(result, args.out, scrub)
            return exit_code(result["verdict"])
        registry = load_registry(args.windows)
        origin, admin = load_secrets(args.state_dir, scrub)
        base = check_target(args.base_url or f"https://{registry['api_app']}.fly.dev", registry["api_app"])
        if args.salt_start is None:
            args.salt_start = int(now()) % 900_000
        fly = Fly(registry["api_app"], runner, args.flyctl)
        log = lambda line: print(f"w1b: {scrub(line)}", file=sys.stderr)  # noqa: E731
        with client or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as http:
            ctx = Context(http, base, origin, admin, scrub, boot=args.boot, now=now, sleep=sleep, log=log)
            report = run_boot(args, ctx, fly, pdf=load_pdf(args.pdf), questions=load_questions())
        emit(report, args.out, scrub)
        return exit_code(report["verdict"])
    except (W1bError, OSError, ValueError, KeyError) as exc:
        print(f"w1b: error: {scrub(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

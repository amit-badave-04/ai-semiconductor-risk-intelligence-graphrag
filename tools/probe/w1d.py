"""The driver of window W1d, the upload-ceiling confirmation: ONE fresh boot of the real service, a burn, ONE max-size upload, no asks.

docs/specs/upload-embed-timeout.md, Plan 2 ("Staging"); ``deploy/staging/windows.json`` "W1d". Run from the repository root, on the developer
machine, against the staging API of the window (``semigraph-stg``, the fp32 image with the change on a fresh shared-cpu-2x / 4 GB machine, the
staging database; NO mock LLM, since no ask is sent):

    python -m tools.probe.w1d boot --boot 1 --pdf small.pdf --out canary.json               # the CANARY: the warm-up machine, a 2-page PDF, before the boot
    python -m tools.probe.w1d boot --boot 1 --wait-for-machine-s 2700 --out boot-1.json     # started BEFORE the deploy
    python -m tools.probe.w1d boot --boot 1 --attempt 2 ... --out boot-1-a2.json            # the one re-run of an INVALID boot
    python -m tools.probe.w1d aggregate boot-*.json --out w1d.json

``boot`` does, for one freshly created machine (the pieces are W1b's: ``tools/probe/w1b.py``): finds it and polls ``/healthz`` from its creation;
waits for the kill level ``off``; starts a ``flyctl logs`` STREAM of the api app into a file (secrets scrubbed before anything is written);
starts the memory sampler; runs the BURN on the machine (``tools/probe/w1d_burn.py``: ``burn.py --burn-s 600`` shipped inside a ``python -c``
command over ``flyctl ssh console``, stopped once at least 300 s have passed and ``report.py`` sees an onset, and confirmed stopped); only then
creates a workspace and uploads the 30-page max-size PDF exactly as the page does, and watches the job to its end; waits for the app's
``upload stages`` log line of that job (``jobs.py`` ``_log_stages``), falling back to a ``--no-tail`` snapshot taken as soon as the job ends; stops the sampler
and the stream and prints ONE JSON object. The burn must have run its 300 s floor, and be confirmed stopped, before the upload is sent. The rules and the verdicts (PASS / FAIL / INVALID / INCONCLUSIVE, fail closed) are in ``tools/probe/w1d_judge.py``.

Secrets: the origin-header secret and the admin token are read from the state directory ``scripts/staging.py`` writes (outside the repository),
used in request headers only, and scrubbed, with the workspace id and token, from everything printed or written, the log file included. Every
flyctl call is an argument list that must name the registered staging API app; the target URL must be that app or loopback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.probe import w1b, w1d_burn, w1d_judge  # noqa: E402
from tools.probe.w1b import Context, Fly, W1bError  # noqa: E402,F401  (W1bError: re-exported, the tests read it from here)
from tools.probe.w1b_judge import Scrub  # noqa: E402,F401  (re-exported)

WINDOW = w1d_judge.WINDOW
# The harness deadline for the job: the budgets of the upload job itself (config.py defaults: parse 90 s, embed ceiling 2,100 s, compare 120 s) and the
# last chunk the ceiling lets through (about 70 s, spec requirement 4), with a margin. W1b's own 1,470 s is the OLD budget and would end the watch
# in the middle of a valid job.
PARSE_S, COMPARE_S, LAST_CHUNK_S, MARGIN_S = 90, 120, 70, 300
JOB_TIMEOUT_S = PARSE_S + w1d_judge.EMBED_CEILING_S + LAST_CHUNK_S + COMPARE_S + MARGIN_S
LOG_WAIT_S, LOG_POLL_S, STREAM_SETTLE_S = 60.0, 3.0, 2.0       # after the job ends: how long the stage line may take to reach the stream
STOP_JOIN_S = 10.0
PDF_PAGES = w1b.PDF_PAGES


# ---- the log stream -----------------------------------------------------------------------------------------------------------------

class LogStream:
    """``flyctl logs`` of the api app as a stream, written line by line to ``path`` AFTER the scrub (a secret never reaches the disk). Started with
    ``--machine`` and, when this flyctl rejects the flag (the process ends at once), for the whole app, which holds exactly one machine here.
    ``stop`` is idempotent. ``error`` says why no stream runs, if none does."""

    def __init__(self, fly: Fly, path: Path, scrub: Scrub, popen: Callable[..., Any] = subprocess.Popen, sleep: Callable[[float], None] = time.sleep):
        self.fly, self.path, self.scrub, self.popen, self.sleep = fly, Path(path), scrub, popen, sleep
        self.proc, self.thread, self.handle, self.error, self._stopped = None, None, None, None, False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, machine_id: str) -> None:
        if not self.fly.app or self.fly.app in w1b.LIVE_APPS:
            raise W1bError("refusing to stream the logs of an app that is not the registered staging API app")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "w", encoding="utf-8", newline="\n")  # noqa: SIM115  (closed in stop)
        for extra in (["--machine", machine_id], []):
            proc = self.popen([self.fly.flyctl, "logs", "-a", self.fly.app, *extra], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
            self.sleep(STREAM_SETTLE_S)
            if proc.poll() is None:
                self.proc = proc
                self.thread = threading.Thread(target=self._pump, args=(proc,), name="w1d-logs", daemon=True)
                self.thread.start()
                return
        self.error = "flyctl logs ended at once, with and without --machine: the stage line will come from a snapshot"

    def _pump(self, proc: Any) -> None:
        for raw in proc.stdout:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            self.handle.write(self.scrub(text))
            self.handle.flush()

    def text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=STOP_JOIN_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.thread is not None:
            self.thread.join(STOP_JOIN_S)
        if self.handle is not None:
            self.handle.close()
            self._sweep()

    def _sweep(self) -> None:
        """Scrub the whole file once more now that every secret is known: the workspace id exists only from the moment it is created, so a line
        that the app logged just before the driver learned it could have been written unscrubbed."""
        text = self.text()
        scrubbed = self.scrub(text)
        if scrubbed != text:
            self.path.write_text(scrubbed, encoding="utf-8", newline="\n")


def wait_for_line(stream: LogStream, job_id: str, *, wait_s: float, poll_s: float, now: Callable[[], float],
                  sleep: Callable[[float], None]) -> dict | None:
    """The stage line of ``job_id`` from the stream, waiting up to ``wait_s`` for it (the app writes it after the job's last event, and the logs lag).
    A stream that is not running is read once."""
    deadline = now() + wait_s
    while True:
        line = w1d_judge.stage_line_for(w1d_judge.parse_stage_lines(stream.text()), job_id)
        if line is not None or not stream.running or now() >= deadline:
            return line
        sleep(poll_s)


def logs_snapshot(fly: Fly, machine_id: str) -> str | None:
    """The app's logs as one ``--no-tail`` snapshot (the machine's own when this flyctl has the flag), raw; ``None`` when it could not be read.
    The snapshot is a short window (a recorded one held exactly 100 lines), so the stage line's fallback takes it as soon as the job ends."""
    result = fly(["logs", "-a", fly.app, "--machine", machine_id, "--no-tail"], timeout=180)
    if result.returncode != 0:
        result = fly(["logs", "-a", fly.app, "--no-tail"], timeout=180)
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None


def collect_evidence(fly: Fly, machine_id: str, scrub: Scrub) -> tuple[dict, str | None]:
    """Machine events and the app's logs (one snapshot): the same evidence as W1b's ``collect_machine_evidence``, with the snapshot's text kept
    for the stage line's fallback. Each source is ``None`` when it could not be read."""
    status = w1b_events(fly, machine_id)
    text = logs_snapshot(fly, machine_id)
    logs = w1b.analyze_logs(text, machine_id, scrub) if text is not None else None
    return {"events": status, "logs": logs}, (scrub(text) if text is not None else None)


def w1b_events(fly: Fly, machine_id: str) -> dict | None:
    return w1b.analyze_events(w1b._json(fly(["machine", "status", machine_id, "-a", fly.app, "--json"])))


# ---- the upload -----------------------------------------------------------------------------------------------------------------------

def _not_started(why: str) -> dict:
    return {"state": "not_started", "error": why}


def run_upload(ctx: Context, pdf: bytes, *, job_timeout_s: float) -> dict:
    """Create a workspace, upload the PDF exactly as the page does and watch the job to its end. NO ask is sent. ``ready_s`` is from the upload's
    acceptance to the ready event (W1b's definition), ``end_s`` to any terminal event. The workspace id and token live in this function only."""
    ws_ip = f"172.16.{ctx.boot}.{int(ctx.now()) % 250 + 1}"
    created, _ = ctx.retrying("POST", "/api/workspace", ws_ip, ok=(201,), json={"turnstile_token": w1b.STUB_TURNSTILE}, timeout=30.0)
    if created is None or created.status_code != 201:
        return _not_started(f"workspace create answered {getattr(created, 'status_code', None)}")
    info = created.json()
    ws, token = info["workspace_id"], info["token"]
    ctx.scrub.add(ws, token)
    try:
        uploaded, _ = ctx.retrying("POST", f"/api/workspace/{ws}/documents", ws_ip, ok=(202,),
                                   files={"file": ("w1d-max-size.pdf", pdf, "application/pdf")}, data={"title": "W1d max-size upload"},
                                   timeout=httpx.Timeout(120.0, connect=10.0),
                                   extra_headers={"X-Workspace-Token": token, "X-Turnstile-Token": w1b.STUB_TURNSTILE})
        if uploaded is None or uploaded.status_code != 202:
            return _not_started(f"upload answered {getattr(uploaded, 'status_code', None)}")
        accepted, job_id = ctx.now(), uploaded.json()["job_id"]
        result = w1b.watch_job(ctx, ws, token, job_id, ws_ip, deadline=accepted + job_timeout_s)
        ended = ctx.now()
        return {**result, "job_id": job_id, "accepted_at": accepted, "end_s": round(ended - accepted, 1),
                "ready_s": round(ended - accepted, 1) if result["state"] == "ready" else None}
    finally:
        ctx.request("DELETE", f"/api/workspace/{ws}", ws_ip, extra_headers={"X-Workspace-Token": token}, timeout=30.0)


def read_embedder_variant(ctx: Context) -> str | None:
    """Which embedder the machine loaded (``/healthz`` ``embedder_variant``): recorded, not judged."""
    response = ctx.request("GET", "/healthz", "10.0.0.1", timeout=5.0)
    try:
        return response.json().get("embedder_variant") if response is not None and response.status_code == 200 else None
    except ValueError:
        return None


# ---- one boot --------------------------------------------------------------------------------------------------------------------------

def workload_plan(registry: dict, args: argparse.Namespace) -> dict:
    """What the boot will do, decided before anything is created or requested: only the upload-only workload the window registers (no ask, the burn of
    300 to 600 s); ``--burn-s`` and ``--min-burn-s`` may not differ from it."""
    registered = registry.get("workload") or {}
    if registered.get("mode") != "upload_only" or registered.get("asks") != 0:
        raise W1bError(f"windows.json does not register the upload-only workload for window {WINDOW}")
    burn_s = registered["burn_s"] if args.burn_s is None else args.burn_s
    min_s = registered["min_burn_s"] if args.min_burn_s is None else args.min_burn_s
    if (burn_s, min_s) != (registered["burn_s"], registered["min_burn_s"]):
        raise W1bError(f"window {WINDOW} registers a burn of {registered['burn_s']} s, stopped from {registered['min_burn_s']} s: "
                       "--burn-s and --min-burn-s may not change them")
    return dict(registered)


def measure(ctx: Context, fly: Fly, args: argparse.Namespace, plan: dict, pdf: bytes) -> dict:
    """The burn, then the upload: the upload only after the burn has run at least its floor (``min_burn_s``) and is confirmed stopped (the quota is
    pooled, a burn would take the embed's share; a burn that was cut short has not used the balance up)."""
    started = ctx.now()
    burn_result = w1d_burn.run_burn_step(fly, now=ctx.now, sleep=ctx.sleep, log=ctx.log, burn_s=plan["burn_s"], min_s=plan["min_burn_s"])
    if not burn_result["confirmed_stopped"]:
        job = _not_started("the burn could not be confirmed stopped: no upload was sent beside it")
    elif burn_result["burn_seconds"] < plan["min_burn_s"]:
        # stopped is not the same as run: reads that failed at the start, or a burn killed early, also end with a confirmed stop and a short burn
        job = _not_started(f"the burn did not run its minimum {plan['min_burn_s']} s (it ran {burn_result['burn_seconds']} s): no upload was sent")
    else:
        job = run_upload(ctx, pdf, job_timeout_s=args.job_timeout_s)
        accepted = job.get("accepted_at")
        if accepted is not None:
            # the idle gap the spec cares about (the balance refills during rest) starts at the stop request, not at the end of the
            # confirmation reads that follow it; the machine's own clock gives it from the last sample the burn wrote
            job["burn_stop_to_accept_s"] = round(accepted - burn_result["stop_requested_at"], 1)
            job["burn_end_to_accept_s"] = round(accepted - burn_result["ended_at"], 1)
            if burn_result.get("last_sample_wall") is not None:
                job["burn_last_sample_to_accept_s"] = round(accepted - burn_result["last_sample_wall"], 1)
    job.pop("accepted_at", None)
    burn_result.pop("ended_at", None)
    burn_result.pop("stop_requested_at", None)
    return {"burn": burn_result, "job": job, "workload_s": round(ctx.now() - started, 1)}


def stage_record(line: dict | None, snapshots: Sequence[str | None], job_id: str | None, stream: LogStream) -> dict:
    """The stage line of the job: the stream's, else the first of the snapshots (the early one, then the final one) that holds it."""
    source = "stream" if line is not None else None
    if line is None and job_id:
        for snapshot in snapshots:
            line = w1d_judge.stage_line_for(w1d_judge.parse_stage_lines(snapshot), job_id) if snapshot else None
            if line is not None:
                source = "snapshot"
                break
    return {"source": source, "fields": None if line is None else line["fields"], "job_id": job_id, "log_file": str(stream.path),
            "stream_error": stream.error}


def run_boot(args: argparse.Namespace, ctx: Context, fly: Fly, *, pdf: bytes, plan: dict, stream: LogStream) -> dict:
    machine = w1b.wait_for_machine(fly, args.machine_id, wait_s=args.wait_for_machine_s, not_before=ctx.now() - w1b.MACHINE_AGE_SLACK_S,
                                   now=ctx.now, sleep=ctx.sleep)
    started_at = w1b.parse_time(args.boot_started_at if args.boot_started_at != "auto" else machine["created_at"] or "")
    report: dict = {"window": WINDOW, "boot": args.boot, "attempt": args.attempt, "workload": plan, "machine": machine,
                    "boot_started_at": datetime.fromtimestamp(started_at, UTC).isoformat(timespec="seconds"),
                    "pdf": {"sha256": hashlib.sha256(pdf).hexdigest(), "bytes": len(pdf), "pages": PDF_PAGES},
                    "limits": {"vmhwm_kb": w1d_judge.VMHWM_LIMIT_KB, "share_limit": float(w1d_judge.SHARE_LIMIT),
                               "embed_over_ready": float(w1d_judge.EMBED_READY_RATIO), "job_timeout_s": args.job_timeout_s}}
    report["healthz"] = w1b.wait_healthy(ctx, started_at)
    sampler_started, line, job, early = False, None, None, None
    try:
        if report["healthz"]["boot_to_healthy_s"] is not None:
            report["kill_level"] = w1b.wait_kill_level_off(ctx)
            report["embedder_variant"] = read_embedder_variant(ctx)
            try:
                stream.start(machine["id"])                    # before the burn and the upload: the stage line is written when the job ends
                sampler_started = True                         # even a start that failed half way gets a stop and a read below
                w1b.start_sampler(fly, sleep=ctx.sleep, log=ctx.log,
                                  duration_s=w1b.sampler_duration_s(args.job_timeout_s + plan["burn_s"] + w1d_burn.BURN_SLACK_S, 0.0))
                report.update(measure(ctx, fly, args, plan, pdf))
            except Exception as exc:  # noqa: BLE001  a boot that cannot be measured is reported (INCONCLUSIVE), never lost to a traceback
                report["job"] = _not_started(ctx.scrub(f"{type(exc).__name__}: {exc}"))
            job = report.get("job") or {}
            if job.get("job_id"):
                line = wait_for_line(stream, job["job_id"], wait_s=LOG_WAIT_S, poll_s=LOG_POLL_S, now=ctx.now, sleep=ctx.sleep)
                if line is None:                               # the stream never ran, died in the middle or stayed silent: the snapshot at once, while the line
                    early = ctx.scrub(logs_snapshot(fly, machine["id"]) or "")    # is still inside its short window (the evidence snapshot comes later)
    finally:
        stream.stop()
    collected = w1b.stop_and_collect_sampler(fly, sleep=ctx.sleep) if sampler_started else None
    summary = w1b.summarize_samples(*collected) if collected else None
    evidence, snapshot = collect_evidence(fly, machine["id"], ctx.scrub)
    report["memory"] = summary or {}
    report["oom_or_restart"] = w1b.judge_oom_or_restart(summary, evidence, ctx.scrub)
    report["max_vmhwm_kb"] = report["memory"].get("max_vmhwm_kb")
    report["stages"] = stage_record(line, (early, snapshot), (job or {}).get("job_id"), stream)
    report["verdict"], report["reasons"], report["criteria"] = w1d_judge.boot_verdict(report)
    report["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return report


# ---- the command line ---------------------------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="w1d", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("boot", help="measure ONE freshly created machine and print its JSON")
    boot.add_argument("--attempt", type=int, default=1, choices=(1, 2), help="2 = the one unchanged re-run of an INVALID boot (the stop rule)")
    boot.add_argument("--boot", type=int, default=1, choices=(1,), help="window W1d is one boot")
    boot.add_argument("--base-url", default=None, help="default: the window's staging API; only that or loopback is accepted")
    boot.add_argument("--machine-id", default=None)
    boot.add_argument("--wait-for-machine-s", type=float, default=0.0,
                      help="start BEFORE the deploy and wait this long for the new machine to appear (0: it must exist now)")
    boot.add_argument("--boot-started-at", default="auto", help="ISO time of the machine's creation; auto = created_at from flyctl")
    boot.add_argument("--state-dir", type=Path, default=None, help="where scripts/staging.py keeps the window's secrets")
    boot.add_argument("--burn-s", type=int, default=None, help="the burn's cap on the machine; the window registers it (600) and refuses another")
    boot.add_argument("--min-burn-s", type=int, default=None, help="the burn runs at least this long before the onset can stop it (300, registered)")
    boot.add_argument("--job-timeout-s", type=float, default=JOB_TIMEOUT_S)
    boot.add_argument("--pdf", type=Path, default=None, help="use this file instead of the max-size fixture")
    boot.add_argument("--windows", type=Path, default=w1b.WINDOWS_JSON)
    boot.add_argument("--flyctl", default="flyctl")
    boot.add_argument("--out", type=Path, default=None)
    boot.add_argument("--logs-out", type=Path, default=None, help="the scrubbed log stream's file (default: beside --out, as NAME.logs.txt)")
    agg = sub.add_parser("aggregate", help="the W1d verdict of its boot files (every attempt)")
    agg.add_argument("files", nargs="+", type=Path)
    agg.add_argument("--out", type=Path, default=None)
    return parser


def logs_path(args: argparse.Namespace) -> Path:
    if args.logs_out is not None:
        return args.logs_out
    return args.out.with_name(f"{args.out.stem}.logs.txt") if args.out is not None else Path(f"w1d-boot-{args.boot}-a{args.attempt}.logs.txt")


def main(argv: Sequence[str] | None = None, *, runner: Callable[[list[str], float], w1b.Result] = w1b.run_subprocess,
         popen: Callable[..., Any] = subprocess.Popen, client: httpx.Client | None = None, now: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep) -> int:
    args = build_parser().parse_args(argv)
    scrub = Scrub()
    try:
        if args.command == "aggregate":
            result = w1d_judge.aggregate([json.loads(Path(p).read_text(encoding="utf-8")) for p in args.files])
            w1b.emit(result, args.out, scrub)
            return w1b.exit_code(result["verdict"])
        registry = w1b.load_registry(args.windows, WINDOW)
        plan = workload_plan(registry, args)
        origin, admin = w1b.load_secrets(args.state_dir, scrub, WINDOW)
        base = w1b.check_target(args.base_url or f"https://{registry['api_app']}.fly.dev", registry["api_app"])
        fly = Fly(registry["api_app"], runner, args.flyctl)
        log = lambda line: print(f"w1d: {scrub(line)}", file=sys.stderr)  # noqa: E731
        stream = LogStream(fly, logs_path(args), scrub, popen=popen, sleep=sleep)
        with client or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as http:
            ctx = Context(http, base, origin, admin, scrub, boot=args.boot, now=now, sleep=sleep, log=log)
            report = run_boot(args, ctx, fly, pdf=w1b.load_pdf(args.pdf), plan=plan, stream=stream)
        w1b.emit(report, args.out, scrub)
        return w1b.exit_code(report["verdict"])
    except (W1bError, OSError, ValueError, KeyError) as exc:
        print(f"w1d: error: {scrub(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

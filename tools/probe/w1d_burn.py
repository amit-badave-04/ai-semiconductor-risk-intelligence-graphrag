"""The burn step of window W1d (docs/specs/upload-embed-timeout.md, Plan 2 "Burn step"): use up the api machine's burst balance before the
upload, so that the upload meets the throttled share this window tests.

The burn must run ON the api machine (the CPU quota is the machine's, pooled across its vCPUs), and no file may be uploaded to it. So
``tools/probe/burn.py`` (one full-core loop, one sample a second, stdlib only) is shipped as text inside a ``python -c`` command over
``flyctl ssh console -C``, exactly as ``w1b.py`` ships ``memsample.py``: that path already ran on Fly in windows W1b and W1c, it needs
no stdin and no second app. The command detaches the burn (``--daemon``) so the ssh call returns, caps it at ``--burn-s 600`` on the
machine itself (it ends there even if this driver dies) and gives it a stop file (``burn.py`` looks for it once a second). The tools
image is not used: it is another app on another machine with its own quota.

The driver polls the burn's samples (``tail -n +K``: by line number, so a banner or a line ending cannot shift the offset), applies the
spec's rule locally with ``tools/probe/report.py`` (``should_stop``: at least 300 s of burn AND an onset, below 90 % of the baseline for
5 s in a row) and, when it holds, creates the stop file. It then READS to the end and checks that nothing more is written: the upload
never runs beside the burn (the quota is pooled, a burn would take the embed's share), so a burn that cannot be confirmed stopped ends the
boot. If no onset shows the burn ends at its 600 s cap (the Fly page's 500 s maximum balance drains at about 0.875 s per s: 571 s,
inferred) and the upload is sent anyway: the validity rule of ``w1d_judge`` decides whether the boot counts, not the burn.

"Confirmed stopped" says nothing about how long the burn ran: reads that failed at the start, or a burn killed early (OOM), also end with a stop
that is confirmed. So the step reports ``burn_seconds`` and the caller (``w1d.measure``) uploads only after a burn of at least the 300 s floor.
A burn is ``not_started`` only when the machine ANSWERED a read and has no sample 45 s after the start; reads that merely fail (a cold ``flyctl
ssh`` start, a timeout) prove nothing about the burn, so they keep the step polling until the deadline.

Pure functions (``burn_command``, ``SampleTail``, ``should_stop``) and one step (``run_burn_step``) whose clock, sleep and flyctl are
parameters.
"""

from __future__ import annotations

import base64
import json
import shlex
import zlib
from collections.abc import Callable
from pathlib import Path

from tools.probe import burn, report
from tools.probe.w1b import BOOTSTRAP, Fly

BURN_DIR, BURN_SAMPLES, BURN_STOP = "/tmp/w1d-burn", "/tmp/w1d-burn/samples.jsonl", "/tmp/w1d-burn.stop"
DEFAULT_BURN_S, DEFAULT_MIN_BURN_S = 600, 300      # the spec: burn.py --burn-s 600, stopped early from 300 s once the onset shows
POLL_S = 20.0                                       # between two reads of the samples (each read is an ssh connection)
START_WAIT_S = 45.0                                 # the burn must have written a sample by then, or it did not start
BURN_SLACK_S = 90.0                                 # past the burn's own cap, how long reads may keep failing before the step stops waiting
SETTLE_S = 3.0                                      # after the stop file: the burn notices it within a second
READ_TIMEOUT_S = 90
NO_SUCH_FILE = b"No such file"                      # what tail says when the burn never created its file: the machine answered, there is no burn


def burn_source() -> str:
    return Path(burn.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")


def burn_command(source: str, *, burn_s: int = DEFAULT_BURN_S) -> str:
    """The remote command: the burn's text, compressed and base64'd, run by ``exec`` inside ``python -c`` (the one word
    ``sys.argv.pop(1)`` is the payload; the rest are the burn's own options)."""
    blob = base64.b64encode(zlib.compress(source.encode("utf-8"), 9)).decode("ascii")
    return f"python -c {shlex.quote(BOOTSTRAP)} {blob} --daemon --out {BURN_DIR} --burn-s {int(burn_s)} --stop-file {BURN_STOP}"


class SampleTail:
    """The burn's samples as read in pieces. ``command()`` asks for everything after the lines already taken; ``feed(text)`` takes the
    complete sample lines of an answer (anything else, a banner or a torn last line, is skipped and read again next time)."""

    def __init__(self, path: str):
        self.path, self.samples, self.answered = path, [], False   # answered: the machine replied to a read at least once (even "no such file")

    def command(self) -> str:
        return f"tail -n +{len(self.samples) + 1} {self.path}"

    def feed(self, text: str) -> int:
        taken = 0
        complete = text.split("\n")[:-1]                   # a last piece without a newline is torn
        for line in complete:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("t"), int) and "iter_per_s" in row:
                self.samples.append(row)
                taken += 1
        return taken


def burn_seconds(samples: list[dict]) -> int:
    return max((s["t"] for s in samples if s.get("phase", "burn") == "burn"), default=0)


def should_stop(samples: list[dict], *, min_s: int = DEFAULT_MIN_BURN_S) -> bool:
    """The spec's rule: at least ``min_s`` seconds of burn AND ``report.py`` reports an onset (below 90 % of the baseline for 5 s in a
    row). A pure function of the samples."""
    return burn_seconds(samples) >= min_s and report.analyze(samples)["throttled"] is True


def _read_new(fly: Fly, tail: SampleTail, failures: list[int]) -> int:
    """One read of the new samples: how many were taken, or -1 when the read failed. A read the machine answered (exit 0, or ``tail`` itself
    saying the file does not exist) sets ``tail.answered``; a transport failure does not."""
    result = fly(["ssh", "console", "-a", fly.app, "-C", tail.command()], timeout=READ_TIMEOUT_S)
    if result.returncode != 0:
        failures.append(1)
        if NO_SUCH_FILE in result.stdout + result.stderr:
            tail.answered = True
        return -1
    tail.answered = True
    return tail.feed(result.stdout.decode("utf-8", "replace"))


def _stop_and_confirm(fly: Fly, tail: SampleTail, failures: list[int], sleep: Callable[[float], None]) -> bool:
    """Create the stop file, read what the burn wrote up to its end, then read again: the burn is stopped only when the second read
    works and finds nothing new. A read that fails is not a confirmation."""
    fly(["ssh", "console", "-a", fly.app, "-C", f"touch {BURN_STOP}"], timeout=60)
    sleep(SETTLE_S)
    if _read_new(fly, tail, failures) < 0:
        return False
    sleep(SETTLE_S)
    return _read_new(fly, tail, failures) == 0


def _summary(samples: list[dict], criterion_met: bool) -> dict:
    analysis = report.analyze(samples)
    return {"throttled": analysis["throttled"], "onset_s": analysis["onset_s"], "baseline_iter_s": analysis["baseline_iter_s"],
            "sustained_iter_s": analysis["sustained_iter_s"], "sustained_fraction": analysis["sustained_fraction"],
            "steal": analysis["steal"], "summary": analysis["summary"], "criterion_met": criterion_met}


def run_burn_step(fly: Fly, *, now: Callable[[], float], sleep: Callable[[float], None], log: Callable[[str], None],
                  burn_s: int = DEFAULT_BURN_S, min_s: int = DEFAULT_MIN_BURN_S, poll_s: float = POLL_S,
                  source: str | None = None) -> dict:
    """Start the burn on the machine, watch it, stop it and confirm it stopped; returns what happened, every sample included.
    ``stopped_by`` is ``criterion`` (the onset showed after ``min_s``), ``burn_ended`` (the burn reached its cap), ``deadline`` (the cap and
    the slack passed with neither: reads kept failing, or the burn stopped writing early) or ``not_started`` (the machine answered a read and
    had no sample ``START_WAIT_S`` after the start). ``confirmed_stopped`` False means the burn may still be running: the caller must not upload;
    True with ``burn_seconds`` under ``min_s`` means a short burn: the caller must not upload either."""
    started_at = now()
    start = fly(["ssh", "console", "-a", fly.app, "-C", burn_command(source if source is not None else burn_source(), burn_s=burn_s)], timeout=120)
    started = start.returncode == 0
    if not started:
        log("burn: the start command failed")
    tail, failures, reason = SampleTail(BURN_SAMPLES), [], None
    while reason is None:
        sleep(poll_s)
        _read_new(fly, tail, failures)
        waited = now() - started_at
        if tail.samples and should_stop(tail.samples, min_s=min_s):
            reason = "criterion"
        elif burn_seconds(tail.samples) >= burn_s:
            reason = "burn_ended"
        elif not tail.samples and tail.answered and waited >= START_WAIT_S:
            reason = "not_started"
        elif waited >= burn_s + BURN_SLACK_S:
            reason = "deadline"
    log(f"burn: {len(tail.samples)} samples, stopping ({reason})")
    stop_requested_at = now()                          # the idle gap before the upload starts here: the burn ends within a second of it
    confirmed = _stop_and_confirm(fly, tail, failures, sleep)
    met = should_stop(tail.samples, min_s=min_s)
    log(f"burn: {len(tail.samples)} samples in all, stopped {'and confirmed' if confirmed else 'but NOT confirmed'}")
    return {"started": started, "stopped_by": reason, "burn_s_planned": burn_s, "min_s": min_s, "samples": len(tail.samples),
            "burn_seconds": burn_seconds(tail.samples), "elapsed_s": round(now() - started_at, 1), "confirmed_stopped": confirmed,
            "read_failures": len(failures), "stop_requested_at": stop_requested_at, "ended_at": now(),
            "last_sample_wall": tail.samples[-1].get("wall") if tail.samples else None, **_summary(tail.samples, met),
            "per_second": [{"t": s["t"], "iter_per_s": s["iter_per_s"], "steal_frac": s.get("steal_frac")} for s in tail.samples]}

"""The burn step of window W1d (docs/specs/upload-embed-timeout.md, Plan 2 "Burn step"): use up the api machine's burst balance before the
upload, so that the upload meets the throttled share this window tests.

The burn must run ON the api machine (the CPU quota is the machine's, pooled across its vCPUs), and no file may be uploaded to it. So
``tools/probe/burn.py`` (one full-core loop, one sample a second, stdlib only) is shipped as text inside a ``python -c`` command over
``flyctl ssh console -C``, exactly as ``w1b.py`` ships ``memsample.py``: that path already ran on Fly in windows W1b and W1c, it needs
no stdin and no second app. The command detaches the burn (``--daemon``), caps it at ``--burn-s 600`` on the machine itself (it ends there
even if this driver dies) and gives it an output directory and a stop file that belong to THIS run only (``/tmp/w1d-burn-<run id>``): a samples
file that an earlier burn left on the same machine (the canary's) can never pass for this run's. The tools image is not used: it is another
app on another machine with its own quota.

THE BURN IS NEVER STEERED OVER SSH WHILE IT RUNS. The live canary of 2026-11-06 showed why: a full-core burn uses up the machine's pooled
shared-CPU quota and starves sshd and the ssh helper along with everything else. The driver's reads worked until sample 223 and every one
failed after it, and the stop file could not be created, although the burn itself ran its whole 600 s and wrote a good file. So the early stop on
the onset that Plan 2 specified ("stop it early") is DROPPED, and the step does this instead:

1. start the burn (the one ssh call) and let it run its full ``burn_s`` (600);
2. wait LOCALLY for ``burn_s`` plus a margin (30 s) from the start command's return, with no ssh traffic of any kind;
3. read the whole samples file, wait a few seconds and read once more. A read that fails is tried again 10 s later for up to ``READ_BUDGET_S`` (ten
   minutes): the canary's ssh was still failing minutes after the burn's last write, so the machine is not instantly idle. The burn is CONFIRMED over
   when its last sample is at least second ``burn_s - 1`` and the second read finds no new line;
4. run ``report.py``'s onset analysis on the full samples: it is recorded, and decides nothing.

The burn's seconds DRIFT: ``burn.py`` ends a second's bucket at the first chunk that finishes after it, so under throttling 600 samples take more than
600 s (the canary's timestamps suggest 10 to 70 s more, inferred). The margin can therefore end before the last sample. When the second read finds the
file still growing short of its cap, the step does not touch the burn: it waits LOCALLY, again with no ssh, for the samples that are missing (times
``EXTRA_WAIT_FACTOR`` plus the margin), reads the rest and confirms again, at most ``MAX_EXTRA_WAITS`` times. Only a file that is still growing after
that (or growing past its own cap) gets the stop file, as a FALLBACK: the step creates it, reads on and checks again. An unconfirmed stop sends no
upload (the quota is pooled, a burn would take the embed's share), and neither does a burn that did not run its full length (it died, or was stopped
short): ``confirmed_stopped`` says the burn is not writing, ``burn_complete`` says it ran its ``burn_s``, and the caller (``w1d.measure``) uploads only
when both hold. A burn that never wrote (the machine answered "No such file") is ``not_started``; reads that keep failing are ``read_failed``. The
result carries every read attempt (``reads``: its offset from the end of the wait, ok or not, how many new samples) and the burn's span on the machine's
clock (``burn_span_machine_s``), so the next canary shows how long ssh took to recover and how far the burn drifted.

The samples are read by line number (``tail -n +K``) so a banner or a line ending cannot shift the offset. Two clocks are involved and are never mixed:
the driver's (``confirmed_at``, ``waited_s``, ``elapsed_s``) and the machine's (the ``wall`` of each sample, ``last_sample_wall_machine``).

Pure functions (``burn_command``, ``SampleTail``, ``onset_after_floor``) and one step (``run_burn_step``) whose clock, sleep and flyctl are
parameters.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import shlex
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tools.probe import burn, report
from tools.probe.w1b import BOOTSTRAP, Fly

DEFAULT_BURN_S, DEFAULT_MIN_BURN_S = 600, 300      # the spec: burn.py --burn-s 600; the 300 s floor stays as a last check on what the burn did run
MARGIN_S = 30.0                                     # past the burn's own cap, how long the driver keeps waiting (no ssh) before it reads
WAIT_CHUNK_S = 60.0                                 # the local wait is sliced only to print a progress line
START_TIMEOUT_S = 120                               # the one ssh call that starts the burn
READ_TIMEOUT_S = 45                                 # one try of a read: a try that takes longer is a failure
READ_RETRY_S = 10.0                                 # a read that failed is tried again after this pause ...
READ_BUDGET_S = 600.0                               # ... until it has worked or this long has passed (ssh was still failing minutes after the canary's burn ended)
CONFIRM_GAP_S = 5.0                                 # between the read of the whole file and the read that must find nothing new
MAX_EXTRA_WAITS = 2                                 # a burn still writing short of its cap is waited for locally (no ssh) this many times ...
EXTRA_WAIT_FACTOR = 1.2                             # ... for (samples missing x this + the margin) seconds each: the burn's seconds drift past 1 s
SETTLE_S = 3.0                                      # fallback only: after the stop file, the burn notices it within a second
STOP_TIMEOUT_S = 60                                 # fallback only: the touch command
COMPLETE_TOLERANCE_S = 0                            # complete = its last sample is second burn_s: burn.py always writes t == burn_s at its cap
CONFIRM_DEADLINE_S = 1500.0                         # the whole confirmation (every read, retry and extra wait) ends this long after the wait, below BURN_SLACK_S
BURN_SLACK_S = 1800.0                               # past the burn's own cap: the margin, slow reads (up to READ_BUDGET_S each) and the extra waits; the sampler must outlive it
NO_SUCH_FILE = b"No such file"                      # what tail says when the burn never created its file: the machine answered, there is no burn
RUN_ID = re.compile(r"[A-Za-z0-9]{1,32}")           # the id goes into a shell command: letters and digits only


@dataclass(frozen=True)
class BurnPaths:
    """Where one run's burn writes on the machine: its own directory and stop file, named by ``run_id``."""

    run_id: str

    def __post_init__(self) -> None:
        if not RUN_ID.fullmatch(self.run_id):
            raise ValueError(f"a burn run id is 1 to 32 letters and digits, got {self.run_id!r}")

    @property
    def dir(self) -> str:
        return f"/tmp/w1d-burn-{self.run_id}"

    @property
    def samples(self) -> str:
        return f"{self.dir}/samples.jsonl"

    @property
    def stop(self) -> str:
        return f"/tmp/w1d-burn-{self.run_id}.stop"


def new_run_id() -> str:
    return secrets.token_hex(4)


def burn_source() -> str:
    return Path(burn.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")


def burn_command(source: str, *, paths: BurnPaths, burn_s: int = DEFAULT_BURN_S) -> str:
    """The remote command: the burn's text, compressed and base64'd, run by ``exec`` inside ``python -c`` (the one word
    ``sys.argv.pop(1)`` is the payload; the rest are the burn's own options)."""
    blob = base64.b64encode(zlib.compress(source.encode("utf-8"), 9)).decode("ascii")
    return f"python -c {shlex.quote(BOOTSTRAP)} {blob} --daemon --out {paths.dir} --burn-s {int(burn_s)} --stop-file {paths.stop}"


class SampleTail:
    """The burn's samples as read in pieces. ``command()`` asks for everything after the lines already taken; ``feed(text)`` takes the
    complete sample lines of an answer (anything else, a banner or a torn last line, is skipped and read again next time). ``no_file`` is true
    while the machine's last answer was "No such file"."""

    def __init__(self, path: str):
        self.path, self.samples, self.no_file = path, [], False

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


def onset_after_floor(samples: list[dict], *, min_s: int = DEFAULT_MIN_BURN_S) -> bool:
    """The spec's onset rule as an observation: at least ``min_s`` seconds of burn AND ``report.py`` reports an onset (below 90 % of the baseline
    for 5 s in a row). Recorded in the result; nothing stops the burn on it any more. A pure function of the samples."""
    return burn_seconds(samples) >= min_s and report.analyze(samples)["throttled"] is True


def is_complete(samples: list[dict], burn_s: int) -> bool:
    """The burn ran to its cap: its last sample is at least second ``burn_s - 1``."""
    return burn_seconds(samples) >= burn_s - COMPLETE_TOLERANCE_S


class _Reader:
    """The reads of the samples file after the wait. ``read()`` is one read WITH retries: a try that fails (the machine is starved or slow to recover)
    is repeated ``READ_RETRY_S`` later until one works or ``READ_BUDGET_S`` has passed. ``attempts`` records every try, for the evidence."""

    def __init__(self, fly: Fly, tail: SampleTail, now: Callable[[], float], sleep: Callable[[float], None], origin: float,
                 deadline: float = float("inf")):
        self.fly, self.tail, self.now, self.sleep, self.origin, self.attempts = fly, tail, now, sleep, origin, []
        self.deadline = deadline                     # the confirmation's one overall deadline (driver clock): no read is tried after it

    @property
    def failures(self) -> int:
        return sum(1 for a in self.attempts if not a["ok"])

    def read(self) -> int | None:
        """New samples taken, or ``None``: the machine said there is no such file (asking again would not change that) or the budget is spent."""
        until = min(self.now() + READ_BUDGET_S, self.deadline)
        while True:
            if self.now() >= self.deadline:
                return None
            at = self.now() - self.origin
            result = self.fly(["ssh", "console", "-a", self.fly.app, "-C", self.tail.command()], timeout=READ_TIMEOUT_S)
            ok = result.returncode == 0
            self.tail.no_file = not ok and NO_SUCH_FILE in result.stdout + result.stderr
            taken = self.tail.feed(result.stdout.decode("utf-8", "replace")) if ok else None
            self.attempts.append({"at_s": round(at, 1), "ok": ok, "new": taken, "no_file": self.tail.no_file})
            if ok:
                return taken
            if self.tail.no_file or self.now() + READ_RETRY_S >= until:
                return None
            self.sleep(READ_RETRY_S)


def _stop_by_file(reader: _Reader, sleep: Callable[[float], None], paths: BurnPaths) -> bool:
    """The fallback: create the stop file, read what the burn wrote up to its end, then read again: the burn is stopped only when the second
    read works and finds nothing new. A read that fails is not a confirmation."""
    reader.fly(["ssh", "console", "-a", reader.fly.app, "-C", f"touch {paths.stop}"], timeout=STOP_TIMEOUT_S)
    sleep(SETTLE_S)
    if reader.read() is None:
        return False
    sleep(SETTLE_S)
    return reader.read() == 0


def _wait_quietly(now: Callable[[], float], sleep: Callable[[float], None], log: Callable[[str], None], until: float) -> None:
    """Wait locally until ``until``: no flyctl call, no ssh, nothing that the starved machine would have to answer."""
    while (left := until - now()) > 0:
        sleep(min(left, WAIT_CHUNK_S))
        if until - now() > 0:
            log(f"burn: waiting for the burn and its margin, no ssh traffic ({until - now():.0f} s left)")


def _confirm(reader: _Reader, now: Callable[[], float], sleep: Callable[[float], None], log: Callable[[str], None], paths: BurnPaths, burn_s: int,
             margin_s: float) -> tuple[str, bool]:
    """Read the whole file, then read again a few seconds later until a read finds nothing new; decide. A file still growing short of its cap is waited for
    locally (no ssh) up to ``MAX_EXTRA_WAITS`` times before the stop file is used. ``(stopped_by, confirmed_stopped)``."""
    samples = reader.tail.samples
    if reader.read() is None:
        return ("not_started" if reader.tail.no_file else "read_failed"), False
    for extra_waits in range(MAX_EXTRA_WAITS + 1):
        sleep(CONFIRM_GAP_S)
        grown = reader.read()
        if grown is None:
            return "read_failed", False
        if grown == 0:                                    # nothing new: the burn is not writing. Over its length it ended itself; under it, it died
            return ("burn_ended" if is_complete(samples, burn_s) else "burn_died"), True
        missing = burn_s - burn_seconds(samples)
        if missing <= 0 or extra_waits == MAX_EXTRA_WAITS:
            break                                         # growing past its own cap, or still growing after the extra waits: not a drift
        pause = min(missing * EXTRA_WAIT_FACTOR + margin_s, reader.deadline - now())
        if pause <= 0:
            return "read_failed", False                 # the overall deadline: undecided, so no upload
        log(f"burn: still writing ({burn_seconds(samples)} of {burn_s} s): waiting {pause:.0f} s more, no ssh")
        _wait_quietly(now, sleep, log, now() + pause)
        if reader.read() is None:                         # the catch-up read; the next round's read must find nothing new
            return "read_failed", False
    log(f"burn: still writing after the margin and the extra waits ({burn_seconds(samples)} s): stop file as a fallback")
    return "stop_file", _stop_by_file(reader, sleep, paths)


def _summary(samples: list[dict], min_s: int) -> dict:
    analysis = report.analyze(samples)
    return {"throttled": analysis["throttled"], "onset_s": analysis["onset_s"], "baseline_iter_s": analysis["baseline_iter_s"],
            "sustained_iter_s": analysis["sustained_iter_s"], "sustained_fraction": analysis["sustained_fraction"],
            "steal": analysis["steal"], "summary": analysis["summary"], "onset_after_floor": onset_after_floor(samples, min_s=min_s)}


def run_burn_step(fly: Fly, *, now: Callable[[], float], sleep: Callable[[float], None], log: Callable[[str], None],
                  burn_s: int = DEFAULT_BURN_S, min_s: int = DEFAULT_MIN_BURN_S, margin_s: float = MARGIN_S, source: str | None = None,
                  run_id: str | None = None) -> dict:
    """Start the burn on the machine, wait for it locally (no ssh traffic), read the whole file and confirm the burn is over; returns what happened,
    every sample included. ``stopped_by`` is ``burn_ended`` (it ran to its cap and stopped writing), ``burn_died`` (it stopped writing short of its cap),
    ``stop_file`` (it was still writing after the margin and the fallback stop file was used), ``not_started`` (the machine answered that there is
    no samples file) or ``read_failed`` (every try of a read failed). ``confirmed_stopped`` False means the burn may still be running: the caller
    must not upload; ``burn_complete`` False means a short burn: the caller must not upload either. ``confirmed_at`` is the driver's clock."""
    paths = BurnPaths(run_id or new_run_id())
    started_at = now()
    start = fly(["ssh", "console", "-a", fly.app, "-C", burn_command(source if source is not None else burn_source(), burn_s=burn_s, paths=paths)],
                timeout=START_TIMEOUT_S)
    started = start.returncode == 0
    if not started:
        log("burn: the start command failed (the burn may still have started after the detach: its file is read after the wait)")
    launched_at = now()
    _wait_quietly(now, sleep, log, launched_at + burn_s + margin_s)
    wait_ended_at = now()
    waited_s = round(wait_ended_at - launched_at, 1)
    reader = _Reader(fly, SampleTail(paths.samples), now, sleep, wait_ended_at, deadline=wait_ended_at + CONFIRM_DEADLINE_S)
    stopped_by, confirmed = _confirm(reader, now, sleep, log, paths, burn_s, margin_s)
    confirmed_at = now()
    samples = reader.tail.samples
    walls = [s["wall"] for s in samples if isinstance(s.get("wall"), int | float)]
    log(f"burn: {len(samples)} samples, {stopped_by}, {'confirmed over' if confirmed else 'NOT confirmed over'}")
    return {"started": started, "run_id": paths.run_id, "samples_path": paths.samples, "stopped_by": stopped_by, "burn_s_planned": burn_s,
            "min_s": min_s, "margin_s": margin_s, "waited_s": waited_s, "samples": len(samples), "burn_seconds": burn_seconds(samples),
            "burn_complete": is_complete(samples, burn_s), "elapsed_s": round(confirmed_at - started_at, 1), "confirmed_stopped": confirmed,
            "read_failures": reader.failures, "reads": reader.attempts, "confirmed_at": confirmed_at,
            "last_sample_wall_machine": samples[-1].get("wall") if samples else None,
            "burn_span_machine_s": round(walls[-1] - walls[0], 3) if len(walls) > 1 else None, **_summary(samples, min_s),
            "per_second": [{"t": s["t"], "iter_per_s": s["iter_per_s"], "steal_frac": s.get("steal_frac")} for s in samples]}

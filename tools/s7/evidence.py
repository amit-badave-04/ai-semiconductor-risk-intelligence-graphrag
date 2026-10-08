"""What the database itself says during a level: restarts and out-of-memory kills (stdlib only).

Three sources, any of which may be missing; a source that is missing makes the answer "unknown" (``None``), never "fine":

* ``server.jsonl``: one probe every few seconds written by the replay (``{"t", "ok", "ms", "uptime_ms"}``); the server's own
  uptime going DOWN between two probes is a restart;
* the Fly log of the database app (``logs-<app>.txt`` from ``scripts/staging.py snapshot``): a ``Started.`` line is a start of
  the server, and an out-of-memory line (a Java heap error, a memory-pool error, a kernel kill) is an out-of-memory;
* the machine events (``machines-<app>.json``): a ``start`` event is a start, an exit event that says ``oom_killed`` is a kill.

Only what falls inside the level's wall-clock window ``[wall_start, wall_end]`` counts for that level: a restart before it
(the database was just seeded) or during another level is not this level's. A log line with no timestamp (a stack trace) is
placed at the last timestamp seen above it, so it is never dropped.

COVERAGE. Finding something always counts; finding NOTHING proves something only if the source reaches back to the start of the
level. ``flyctl logs --no-tail`` returns a short recent buffer, so a snapshot taken at the end of a six-hour window does not
cover the first levels, and a machine's event list is truncated the same way. A log (or event list) whose earliest timestamp is
after ``wall_start`` is not named among the ``sources`` of an absence, and says so in ``notes``: a level with no other evidence is
then "not judged", never a pass.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

TIMESTAMP = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?Z")
STARTED = re.compile(r"\bStarted\.")
OUT_OF_MEMORY = re.compile(r"OutOfMemory|Out of memory|MemoryPoolOutOfMemory|oom[-_ ]?kill|Killed process", re.IGNORECASE)
START_EVENT_TYPES = frozenset({"start", "started"})


@dataclass(frozen=True)
class Verdict:
    """``passed`` is True (nothing found, and there was evidence to look at), False (found) or None (no evidence)."""

    passed: bool | None
    found: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    probes: tuple[dict, ...] = ()
    log_lines: tuple[tuple[float | None, str], ...] = ()
    events: tuple[dict, ...] = ()
    have_logs: bool = False
    have_events: bool = False
    sample_statuses: tuple[str, ...] = field(default_factory=tuple)
    log_first: float | None = None                  # the earliest timestamp in the log, epoch seconds
    event_first: float | None = None                # the earliest machine event


def read_probes(path: Path) -> tuple[dict, ...]:
    if not Path(path).is_file():
        return ()
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return tuple(rows)


def _epoch(match: re.Match) -> float:
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp()


def read_logs(directory: Path | None) -> tuple[tuple[float | None, str], ...] | None:
    """``(epoch seconds or None, line)`` for every line of the database's ``logs-*neo4j*.txt``; None when there is no such file."""
    files = sorted(Path(directory).glob("logs-*neo4j*.txt")) if directory else []
    if not files:
        return None
    out: list[tuple[float | None, str]] = []
    for path in files:
        last: float | None = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = TIMESTAMP.match(line)
            if match:
                last = _epoch(match)
            out.append((last, line))
    return tuple(out)


def read_events(directory: Path | None) -> tuple[dict, ...] | None:
    """The events of every machine in ``machines-*neo4j*.json``; None when there is no such file."""
    files = sorted(Path(directory).glob("machines-*neo4j*.json")) if directory else []
    if not files:
        return None
    events: list[dict] = []
    for path in files:
        try:
            machines = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for machine in machines if isinstance(machines, list) else []:
            events += [e for e in (machine.get("events") or []) if isinstance(e, dict)]
    return tuple(events)


def load(run_level_dir: Path, evidence_dir: Path | None, sample_statuses: tuple[str, ...]) -> Evidence:
    logs, events = read_logs(evidence_dir), read_events(evidence_dir)
    stamps = [when for when, _ in logs or () if when is not None]
    event_times = [t for t in (_event_epoch(e) for e in events or ()) if t is not None]
    return Evidence(probes=read_probes(Path(run_level_dir) / "server.jsonl"), log_lines=logs or (), events=events or (),
                    have_logs=logs is not None, have_events=events is not None, sample_statuses=sample_statuses,
                    log_first=min(stamps, default=None), event_first=min(event_times, default=None))


def _event_epoch(event: dict) -> float | None:
    stamp = event.get("timestamp")
    return float(stamp) / 1000.0 if isinstance(stamp, int | float) else None


def _inside(when: float | None, start: float, end: float) -> bool:
    return when is None or start <= when <= end        # an event with no time cannot be ruled out, so it counts


def _flag(node: object, key: str) -> bool:
    """True when ``key`` is truthy anywhere inside the nested event (Fly nests the exit event in its request)."""
    if isinstance(node, dict):
        return bool(node.get(key)) or any(_flag(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_flag(value, key) for value in node)
    return False


def _sources(evidence: Evidence, start: float, probes: bool) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The sources an ABSENCE can rest on, and the notes on the ones that cannot (they do not reach back to ``start``)."""
    sources: list[str] = ["probes"] if probes else []
    notes: list[str] = []
    for name, have, first in (("logs", evidence.have_logs, evidence.log_first), ("events", evidence.have_events, evidence.event_first)):
        if not have:
            continue
        if first is not None and first <= start:
            sources.append(name)
        else:
            notes.append(f"the {name} do not reach back to the start of the level")
    return tuple(sources), tuple(notes)


def restart(evidence: Evidence, start: float, end: float) -> Verdict:
    found: list[str] = []
    uptimes = [p["uptime_ms"] for p in evidence.probes if p.get("ok") and isinstance(p.get("uptime_ms"), int | float)]
    for earlier, later in zip(uptimes, uptimes[1:], strict=False):
        if later < earlier:
            found.append(f"server uptime fell from {earlier / 1000:.0f} s to {later / 1000:.0f} s between two probes")
            break
    for when, line in evidence.log_lines:
        if STARTED.search(line) and when is not None and start <= when <= end:
            found.append("the server log says Started. during the level")
            break
    for event in evidence.events:
        if str(event.get("type", "")).lower() in START_EVENT_TYPES and _inside(_event_epoch(event), start, end):
            found.append("a machine start event during the level")
            break
    sources, notes = _sources(evidence, start, bool(uptimes))
    if found:
        return Verdict(False, tuple(found), sources, notes)
    return Verdict(True if sources else None, (), sources, notes)


def out_of_memory(evidence: Evidence, start: float, end: float) -> Verdict:
    found: list[str] = []
    if any(OUT_OF_MEMORY.search(status) for status in evidence.sample_statuses):
        found.append("an operation failed with a memory error")
    for when, line in evidence.log_lines:
        if OUT_OF_MEMORY.search(line) and when is not None and start <= when <= end:
            found.append("an out-of-memory line in the server log during the level")
            break
    for event in evidence.events:
        if _flag(event, "oom_killed") and _inside(_event_epoch(event), start, end):
            found.append("a machine exit event says oom_killed")
            break
    sources, notes = _sources(evidence, start, bool(evidence.probes))
    if found:
        return Verdict(False, tuple(found), sources, notes)
    return Verdict(True if sources else None, (), sources, notes)

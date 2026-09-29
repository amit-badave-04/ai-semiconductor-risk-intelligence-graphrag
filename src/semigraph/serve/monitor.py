"""Freshness monitor (M4, docs/v2/M4_PLAN.md 4.1): detects and SURFACES what is new at the source; it never ingests (D1).

Imports only stdlib, ``graph.client`` and ``semigraph.ingestion.{edgar, freshness, federal_register}`` / ``universe``
(never ``semigraph.serve.store``, which drags in ``retrieval.answerer`` for no reason a background poller needs — a
tiny local ``SvcFreshness`` / ``SvcLease`` read/write lives here instead; see the module's own queries below). A
missing ``SEC_USER_AGENT`` never fails the boot: :attr:`FreshnessMonitor.configured` is False and the monitor idles.

``check_once`` builds its own retrying SEC fetcher (20 s timeout, the 0.15 s fair-access pause, wrapped in
``federal_register.with_retries``) rather than relying on ``ingestion.freshness.fetch_submissions``'s own default
network path, which uses a 30 s timeout and no retries — the plan's own numbers for this call are stricter.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

from ..graph.client import run_cypher
from ..ingestion import federal_register
from ..ingestion.edgar import FilingRecord, select_targets
from ..ingestion.freshness import fetch_submissions, records_from_submissions
from ..universe import ANNUAL_SINCE, FILERS

logger = logging.getLogger("semigraph.serve.monitor")

SEC_FETCH_TIMEOUT_S = 20
SEC_FETCH_PAUSE_S = 0.15
LEASE_MINUTES = 30
# docs/v2/M4_PLAN.md 15.10: a failed check is retried sooner than a full `freshness_poll_hours` wait, so a transient
# SEC/Federal-Register outage clears itself well inside one poll interval instead of sitting at `error` for hours.
ERROR_RETRY_MINUTES = 30

COMPANY_CIK_QUERY = "MATCH (c:Company) WHERE c.ticker IS NOT NULL RETURN c.ticker AS ticker, c.cik AS cik"
KNOWN_ACCESSIONS_QUERY = "MATCH (:Company)-[:FILED]->(f:Filing) RETURN f.accession_no AS accession_no"
EXPORT_CONTROL_COUNT_QUERY = "MATCH (x:ExportControl) RETURN count(x) AS n"
# The newest Snapshot: when the SERVED graph's data was built — never the same thing as `as_of` (the comparison
# horizon a check runs with, "today" by default). Conflating the two would have the public page claim "Data as of
# <today>" for a graph actually built days or weeks earlier.
SNAPSHOT_QUERY = "MATCH (s:Snapshot) RETURN s.id AS id, toString(s.as_of) AS as_of ORDER BY s.created_at DESC LIMIT 1"

# The lost-update-safe pattern of ``serve.store.reserve_daily_upload``: the unconditional SET takes the node's write
# lock BEFORE the WHERE filters on it, so two machines racing for the lease cannot both see it as free. No
# uniqueness constraint backs ``SvcLease.key`` (``store.ensure_indexes`` is a forbidden file) — a seam, not a fix
# available here; a brief double-create is a freshness-monitor inconvenience, never a data-safety issue.
LEASE_QUERY = """MERGE (l:SvcLease {key: 'freshness'})
ON CREATE SET l.holder = null, l.until = null
SET l._lock = true
WITH l WHERE l.until IS NULL OR l.until < $now OR l.holder = $me
SET l.holder = $me, l.until = $now + duration({minutes: $minutes})
RETURN l.holder = $me AS ok"""

GET_FRESHNESS_QUERY = """MATCH (f:SvcFreshness {key: 'latest'})
RETURN f.checked_at AS checked_at, f.as_of AS as_of, f.snapshot_id AS snapshot_id,
       f.snapshot_as_of AS snapshot_as_of, f.status AS status, f.error AS error, f.last_error_at AS last_error_at,
       f.pending_json AS pending_json, f.fr_json AS fr_json, f.unresolved_json AS unresolved_json,
       f.duration_s AS duration_s, f.pending_count AS pending_count"""

PUT_FRESHNESS_QUERY = """MERGE (f:SvcFreshness {key: 'latest'})
SET f.checked_at = $checked_at, f.as_of = $as_of, f.snapshot_id = $snapshot_id,
    f.snapshot_as_of = $snapshot_as_of, f.status = $status, f.error = $error, f.last_error_at = $last_error_at,
    f.pending_json = $pending_json, f.fr_json = $fr_json, f.unresolved_json = $unresolved_json,
    f.duration_s = $duration_s, f.pending_count = $pending_count"""


class MonitorBusy(Exception):
    """Raised by :meth:`FreshnessMonitor.check_now` when a check (background or admin) is already running."""


def _default_sec_fetch(settings):
    """A retrying ``fetch(url) -> dict`` with the declared SEC identity and the fair-access pause."""
    identity = settings.sec_user_agent.strip() or "semigraph"

    def _get(url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": identity})
        with urllib.request.urlopen(req, timeout=SEC_FETCH_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        time.sleep(SEC_FETCH_PAUSE_S)
        return data

    return federal_register.with_retries(_get)


def _pending_row(rec: FilingRecord) -> dict:
    return {"ticker": rec.ticker, "cik": rec.cik, "form": rec.form, "filing_date": rec.filing_date.isoformat(),
            "accession_no": rec.accession_no, "period_of_report": rec.period_of_report}


def _served_graph_state(driver) -> tuple[dict[str, int], set[str]]:
    ciks = {r["ticker"]: r["cik"] for r in run_cypher(driver, COMPANY_CIK_QUERY) if r["cik"] is not None}
    known = {r["accession_no"] for r in run_cypher(driver, KNOWN_ACCESSIONS_QUERY)}
    return ciks, known


def _pending_for_ticker(ticker: str, cik: int, fetch, as_of, known_accessions: set[str]) -> list[dict]:
    _, annual_form, quarterly_form = FILERS[ticker]
    submissions = fetch_submissions(cik, fetch=fetch)
    records = records_from_submissions(ticker, cik, submissions)
    targets = select_targets(records, annual_form, quarterly_form, ANNUAL_SINCE, as_of)
    return [_pending_row(r) for r in targets if r.accession_no not in known_accessions]


def check_once(driver, settings, *, fetch=None, today=None) -> dict:
    """One live pass: EDGAR submissions per filer vs ``Filing`` nodes in the SERVED graph, Federal Register live
    count vs ``ExportControl`` nodes. Pure given ``fetch`` (no sleeps, no identity check, no retries — exactly what
    the test suite and ``scripts/freshness_parity.py`` inject). Returns the check's own result fields; ``configured``
    / ``enabled`` / ``status`` / ``next_check_at`` are monitor-level, time-dependent concerns layered on top by
    :meth:`FreshnessMonitor.status_payload`, not by this pure function.
    """
    started = time.monotonic()
    checked_at = datetime.now(UTC)
    as_of = today.isoformat() if hasattr(today, "isoformat") else (today or checked_at.date().isoformat())
    as_of_date = datetime.fromisoformat(as_of).date() if isinstance(as_of, str) else as_of
    real_fetch = fetch if fetch is not None else _default_sec_fetch(settings)

    ciks, known_accessions = _served_graph_state(driver)
    pending: list[dict] = []
    unresolved: list[str] = []
    for ticker in FILERS:
        cik = ciks.get(ticker)
        if cik is None:
            unresolved.append(ticker)
            continue
        pending.extend(_pending_for_ticker(ticker, cik, real_fetch, as_of_date, known_accessions))

    # The SAME fetcher as the SEC calls above — one unified fair-access policy (20 s timeout, with_retries) for
    # every request this pass makes. `federal_register.make_fetcher` also works but defaults to a 30 s timeout,
    # stricter than section 4.1 asks for.
    live_count = int(real_fetch(federal_register.count_query_url(as_of_date))["count"])
    graph_count = run_cypher(driver, EXPORT_CONTROL_COUNT_QUERY)[0]["n"]
    snapshot = run_cypher(driver, SNAPSHOT_QUERY)
    snapshot_id, snapshot_as_of = (snapshot[0]["id"], snapshot[0]["as_of"]) if snapshot else (None, None)

    return {
        "checked_at": checked_at.isoformat(),
        "as_of": as_of,
        "snapshot_id": snapshot_id,
        "snapshot_as_of": snapshot_as_of,
        "pending_count": len(pending),
        "pending_filings": pending,
        "federal_register": {"graph_count": graph_count, "live_count": live_count,
                              "new_since": max(0, live_count - graph_count)},
        "unresolved": sorted(unresolved),
        "duration_s": round(time.monotonic() - started, 3),
    }


def _acquire_lease(driver, holder: str) -> bool:
    rows = run_cypher(driver, LEASE_QUERY, me=holder, now=datetime.now(UTC), minutes=LEASE_MINUTES)
    return bool(rows) and bool(rows[0]["ok"])


def _load_persisted(driver) -> dict | None:
    try:
        rows = run_cypher(driver, GET_FRESHNESS_QUERY)
    except Exception:  # noqa: BLE001 - a bad read must never crash the monitor thread
        logger.exception("loading the persisted freshness state failed")
        return None
    if not rows:
        return None
    r = rows[0]
    # No SvcFreshness row has EVER been written (never a good check, never a failed attempt) — distinct from a row
    # that exists but has only ever recorded errors, whose ``checked_at`` is null too but ``last_error_at`` is not
    # (docs/v2/M4_PLAN.md 15.10: that state must still be loaded, not discarded as "no history").
    if r["checked_at"] is None and r.get("last_error_at") is None:
        return None
    return {"checked_at": r["checked_at"], "as_of": r["as_of"], "snapshot_id": r["snapshot_id"],
            "snapshot_as_of": r["snapshot_as_of"], "status": r["status"], "error": r["error"],
            "last_error_at": r.get("last_error_at"), "pending_count": r["pending_count"] or 0,
            "pending_filings": json.loads(r["pending_json"]) if r["pending_json"] else [],
            "federal_register": json.loads(r["fr_json"]) if r["fr_json"] else None,
            "unresolved": json.loads(r["unresolved_json"]) if r["unresolved_json"] else [],
            "duration_s": r["duration_s"]}


def _persist(driver, result: dict) -> None:
    try:
        run_cypher(driver, PUT_FRESHNESS_QUERY, checked_at=result.get("checked_at"), as_of=result.get("as_of"),
                   snapshot_id=result.get("snapshot_id"), snapshot_as_of=result.get("snapshot_as_of"),
                   status=result.get("status"), error=result.get("error"),
                   last_error_at=result.get("last_error_at"),
                   pending_json=json.dumps(result.get("pending_filings", []), default=str),
                   fr_json=json.dumps(result["federal_register"]) if result.get("federal_register") else None,
                   unresolved_json=json.dumps(result.get("unresolved", [])),
                   duration_s=result.get("duration_s"), pending_count=result.get("pending_count", 0))
    except Exception:  # noqa: BLE001 - a bad write must never crash the monitor thread
        logger.exception("persisting the freshness result failed")


_EMPTY_RESULT = {"checked_at": None, "as_of": None, "snapshot_id": None, "snapshot_as_of": None, "pending_count": 0,
                 "pending_filings": [], "federal_register": None, "unresolved": [], "duration_s": None}


def _error_result(previous: dict | None, exc: Exception) -> dict:
    """A failed check must never look like "0 filings pending" (docs/v2/M4_PLAN.md 15.10): every field of the last
    GOOD result is carried forward unchanged (or an all-empty shape when there has never been one), and only
    ``status``, ``error`` and ``last_error_at`` are ever set here. Chaining holds across repeated failures too: a
    second error's ``previous`` is the first error's OWN carried-forward good fields, so ``checked_at`` never
    silently starts moving just because the checks keep failing."""
    base = {k: v for k, v in (previous or {}).items() if k not in ("status", "error", "last_error_at")}
    return {**_EMPTY_RESULT, **base, "status": "error", "error": str(exc),
            "last_error_at": datetime.now(UTC).isoformat()}


class FreshnessMonitor:
    """A daemon thread that polls EDGAR + the Federal Register at most once every ``poll_hours``, one machine at a
    time (the ``SvcLease``), and persists the result to ``SvcFreshness``. ``summary()`` and ``status_payload()`` are
    in-memory reads only — safe to call synchronously from the event loop (``GET /api/stats``, ``GET /api/freshness``)."""

    def __init__(self, driver, settings):
        self.driver = driver
        self.settings = settings
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._busy = threading.Lock()
        self._last: dict | None = None
        self._machine_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._started_at = datetime.now(UTC)   # for next_check_at before any attempt has ever been made

    @property
    def configured(self) -> bool:
        return bool(self.settings.sec_user_agent.strip())

    @property
    def checking(self) -> bool:
        return self._busy.locked()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="freshness-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        with self._state_lock:
            self._last = _load_persisted(self.driver)   # first action ON THE THREAD, never on the event loop
        if self._stop_event.wait(self.settings.freshness_boot_delay_s):
            return
        if self._is_stale_or_missing():
            self._safe_try_check()
        # ANCHORED on the same due time status_payload()'s next_check_at reports (docs/v2/M4_PLAN.md 15.10),
        # recomputed fresh on every wake — never a flat interval restarted from "now". A monitor that boots with an
        # already-somewhat-stale good check (say 5 h into a 6 h poll interval) must wait only the REMAINING 1 h, not
        # a further full 6 h; a check that just failed must wake again after ERROR_RETRY_MINUTES from the failure,
        # not from whenever the loop happens to next look.
        while not self._stop_event.wait(max(0.0, self._next_wait_seconds())):
            self._safe_try_check()

    def _next_wait_seconds(self) -> float:
        """Seconds from NOW until the next check is due — the single source of truth :meth:`_next_check_at` and
        :meth:`_is_stale_or_missing` both defer to, so the loop's real behaviour, the boot decision and the
        publicly reported ``next_check_at`` can never drift apart from one another. A negative return means a
        check is already overdue."""
        with self._state_lock:
            last = dict(self._last) if self._last else {}
        due_iso = self._next_check_at(last)
        if due_iso is None:   # unconfigured: _try_check() no-ops anyway; just avoid a tight loop
            return max(1, self.settings.freshness_poll_hours) * 3600
        return (datetime.fromisoformat(due_iso) - datetime.now(UTC)).total_seconds()

    def _safe_try_check(self) -> None:
        """``_try_check`` should never raise (every I/O path below is already guarded), but the polling thread must
        survive even a genuinely unexpected error — a dead daemon thread means no more polls until a restart."""
        try:
            self._try_check()
        except Exception:  # noqa: BLE001 - see the docstring
            logger.exception("the freshness poll loop hit an unexpected error")

    def _is_stale_or_missing(self) -> bool:
        """True exactly when the schedule says a check is due NOW — derived from :meth:`_next_wait_seconds` so this
        can never encode different age/backoff rules than the loop that actually acts on it."""
        return self._next_wait_seconds() <= 0

    def _try_check(self) -> None:
        """Background-loop path: skip silently when a check is already running, another machine holds the lease, or
        the lease attempt itself fails (a transient Neo4j hiccup must release the lock and let the NEXT poll try
        again, never leave ``_busy`` held forever)."""
        if not self.configured or not self._busy.acquire(blocking=False):
            return
        try:
            admitted = _acquire_lease(self.driver, self._machine_id)
        except Exception:  # noqa: BLE001 - see the docstring
            logger.exception("acquiring the freshness lease failed")
            admitted = False
        if not admitted:
            self._busy.release()
            return
        self._run_check()

    def _run_check(self) -> dict:
        """Assumes ``self._busy`` is already held; always releases it, whoever called this."""
        try:
            try:
                result = {**check_once(self.driver, self.settings), "status": "ok", "error": None,
                          "last_error_at": None}
            except Exception as e:  # noqa: BLE001 - a check that fails must still release the lock and be reported
                logger.exception("freshness check failed")
                with self._state_lock:
                    previous = self._last
                result = _error_result(previous, e)
            _persist(self.driver, result)
            with self._state_lock:
                self._last = result
            return result
        finally:
            self._busy.release()

    def check_now(self, timeout_s: int = 120) -> dict:
        """Admin path (``POST /api/admin/freshness/check``). Raises :class:`MonitorBusy` when a check is already
        running. Bounds the CALLER's wait at ``timeout_s``; an overrun check keeps running in the background (the
        lock stays held) and is reported here as an ``error`` rather than blocking past the bound."""
        if not self.configured:
            raise MonitorBusy("not configured")  # callers check `configured` first; this is a defensive guard
        if not self._busy.acquire(blocking=False):
            raise MonitorBusy()
        thread = threading.Thread(target=self._run_check, daemon=True)
        thread.start()
        thread.join(timeout_s)
        if thread.is_alive():
            return {**self.status_payload(), "status": "error",
                    "error": f"the check did not finish within {timeout_s}s (still running in the background)"}
        return self.status_payload()

    def status_payload(self) -> dict:
        """The ``GET /api/freshness`` / admin-check response shape (docs/v2/M4_PLAN.md 4.1, 15.10). In-memory only."""
        with self._state_lock:
            last = dict(self._last) if self._last else {}
        return {
            "configured": self.configured,
            "enabled": self.settings.freshness_enabled,
            "status": self._status_for(last),
            "checked_at": last.get("checked_at"),
            "snapshot_as_of": last.get("snapshot_as_of"),   # the graph's OWN data date — never the check's `as_of`
            "last_error_at": last.get("last_error_at"),
            "next_check_at": self._next_check_at(last),
            "pending_count": last.get("pending_count", 0),
            "pending_filings": last.get("pending_filings", []),
            "federal_register": last.get("federal_register"),
            "unresolved": last.get("unresolved", []),
            "duration_s": last.get("duration_s"),
        }

    def _status_for(self, last: dict) -> str:
        if not self.configured:
            return "unconfigured"
        if not last:
            return "never"
        if last.get("status") == "error":
            return "error"
        checked_at = last.get("checked_at")
        if checked_at:
            age_hours = (datetime.now(UTC) - datetime.fromisoformat(checked_at)).total_seconds() / 3600
            if age_hours > 2 * self.settings.freshness_poll_hours:
                return "stale"
        return "ok"

    def _next_check_at(self, last: dict) -> str | None:
        """When the next attempt is due — null only while the monitor is unconfigured (docs/v2/M4_PLAN.md 15.10);
        a disabled monitor is never represented by a :class:`FreshnessMonitor` instance at all (see
        ``monitor_routes._DISABLED_PAYLOAD``, which is null unconditionally there). Anchored on the last ATTEMPT
        (an error retries sooner than a full poll interval), never recomputed from a fresh "now" on every call, so
        two calls a second apart report the same instant rather than drifting."""
        if not self.configured:
            return None
        if last.get("status") == "error" and last.get("last_error_at"):
            anchor, interval_s = last["last_error_at"], ERROR_RETRY_MINUTES * 60
        elif last.get("checked_at"):
            anchor, interval_s = last["checked_at"], max(1, self.settings.freshness_poll_hours) * 3600
        else:
            anchor, interval_s = self._started_at.isoformat(), self.settings.freshness_boot_delay_s
        return (datetime.fromisoformat(anchor) + timedelta(seconds=interval_s)).isoformat()

    def summary(self) -> dict:
        """``{"status", "checked_at", "pending_count"}`` for ``/api/stats`` — a real shape (``status`` in
        ``never``/``unconfigured``/``error``/``stale``/``ok``) whenever a :class:`FreshnessMonitor` instance exists,
        never ``None`` (docs/v2/M4_PLAN.md 15.10; a monitor object only exists at all when the feature is enabled —
        the "disabled" case with no instance is ``routes``'s own concern). Purely in-memory — never touches the
        database, so it is safe to call synchronously from the event loop."""
        payload = self.status_payload()
        return {"status": payload["status"], "checked_at": payload["checked_at"],
                "pending_count": payload["pending_count"]}


def status_without_a_monitor(settings) -> tuple[str, bool]:
    """The ``status``/``configured`` pair when no :class:`FreshnessMonitor` exists: ``FRESHNESS_ENABLED`` off ("disabled"),
    on without ``SEC_USER_AGENT`` ("unconfigured"), or on and configured but no monitor attached (a hand-built test
    ``app.state``: "never"). ``GET /api/freshness`` and ``/api/stats`` both use it, so they cannot disagree."""
    configured = bool((getattr(settings, "sec_user_agent", "") or "").strip())
    if not getattr(settings, "freshness_enabled", False):
        return "disabled", configured
    return ("unconfigured" if not configured else "never"), configured


def summary_without_a_monitor(settings) -> dict:
    """The ``/api/stats`` freshness block when no monitor exists (the same shape :meth:`FreshnessMonitor.summary` returns)."""
    return {"status": status_without_a_monitor(settings)[0], "checked_at": None, "pending_count": 0}


def start_if_enabled(app) -> None:
    """Create and start the monitor when ``FRESHNESS_ENABLED``; ``app.state.freshness_monitor`` is None otherwise."""
    app.state.freshness_monitor = None
    if not app.state.settings.freshness_enabled:
        return
    monitor = FreshnessMonitor(app.state.driver, app.state.settings)
    monitor.start()
    app.state.freshness_monitor = monitor


def stop(app) -> None:
    """Stop the monitor thread (bounded); a no-op when none runs. Called before the database driver closes."""
    monitor = getattr(app.state, "freshness_monitor", None)
    if monitor is not None:
        monitor.stop()

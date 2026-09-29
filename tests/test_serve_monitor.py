"""``serve.monitor``: the freshness monitor detects and surfaces, never ingests (M4, docs/v2/M4_PLAN.md 4.1).

``check_once`` is tested purely (an injected ``fetch``, no network, no sleeps). The threaded ``FreshnessMonitor`` is
tested for its lifecycle contract (``stop()`` returns promptly, ``check_now`` is exclusive and bounded, ``summary()``
never touches the database) against a fake ``run_cypher`` — never a real Neo4j (that is
``tests/integration/test_workspace_repo_neo4j.py`` and the live G5 gate).
"""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from semigraph.serve import monitor as monitor_mod

FIXTURES = Path(__file__).parent / "fixtures" / "freshness"


class FakeSettings:
    sec_user_agent = "Test Suite test@example.com"
    freshness_enabled = True
    freshness_poll_hours = 6
    freshness_boot_delay_s = 0


def _submissions(forms, dates, accessions, periods=None):
    periods = periods or [""] * len(forms)
    return {"filings": {"recent": {"form": forms, "filingDate": dates, "accessionNumber": accessions,
                                   "reportDate": periods}}}


def _driver_stub(monkeypatch, ciks=None, known=None, fr_count=0, lease_ok=True, snapshot=None, lease_error=None):
    def fake_run_cypher(driver, query, **params):
        if query == monitor_mod.COMPANY_CIK_QUERY:
            return ciks or []
        if query == monitor_mod.KNOWN_ACCESSIONS_QUERY:
            return known or []
        if query == monitor_mod.EXPORT_CONTROL_COUNT_QUERY:
            return [{"n": fr_count}]
        if query == monitor_mod.SNAPSHOT_QUERY:
            return [snapshot] if snapshot else []
        if query == monitor_mod.LEASE_QUERY:
            if lease_error is not None:
                raise lease_error
            return [{"ok": lease_ok}]
        if query == monitor_mod.GET_FRESHNESS_QUERY:
            return []
        if query == monitor_mod.PUT_FRESHNESS_QUERY:
            return []
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(monitor_mod, "run_cypher", fake_run_cypher)


def _stub_check_once(monkeypatch, *, delay=0.0, result=None):
    def fake(driver, settings, **kw):
        if delay:
            time.sleep(delay)
        return result or {"checked_at": datetime.now(UTC).isoformat(), "as_of": "2026-09-24", "pending_count": 0,
                          "pending_filings": [], "federal_register": {"graph_count": 0, "live_count": 0,
                                                                       "new_since": 0}, "unresolved": [],
                          "duration_s": delay}

    monkeypatch.setattr(monitor_mod, "check_once", fake)


# ---------------------------------------------------------------------- check_once: pure

def test_check_once_reads_exactly_the_documented_graph_queries():
    assert "MATCH (c:Company)" in monitor_mod.COMPANY_CIK_QUERY and "c.cik" in monitor_mod.COMPANY_CIK_QUERY
    assert "FILED" in monitor_mod.KNOWN_ACCESSIONS_QUERY and "f.accession_no" in monitor_mod.KNOWN_ACCESSIONS_QUERY
    assert "ExportControl" in monitor_mod.EXPORT_CONTROL_COUNT_QUERY


def test_check_once_lists_targets_not_yet_known_to_the_served_graph(monkeypatch):
    _driver_stub(monkeypatch, ciks=[{"ticker": "NVDA", "cik": 1045810}],
                known=[{"accession_no": "0001045810-25-000001"}], fr_count=3)

    def fetch(url):
        if "submissions" in url:
            return _submissions(["10-K", "10-Q"], ["2026-02-20", "2026-05-20"],
                                ["0001045810-25-000001", "0001045810-26-000002"])
        return {"count": 5}

    result = monitor_mod.check_once(object(), FakeSettings(), fetch=fetch, today="2026-09-24")
    assert result["pending_count"] == 1
    assert result["pending_filings"][0]["accession_no"] == "0001045810-26-000002"
    assert result["pending_filings"][0]["ticker"] == "NVDA"
    assert result["federal_register"] == {"graph_count": 3, "live_count": 5, "new_since": 2}
    assert "NVDA" not in result["unresolved"]  # resolved; every OTHER filer has no cik in this fixture
    assert set(result["unresolved"]) == set(monitor_mod.FILERS) - {"NVDA"}
    assert result["as_of"] == "2026-09-24"
    assert result["duration_s"] >= 0


def test_check_once_lists_a_ticker_absent_from_the_graph_as_unresolved(monkeypatch):
    _driver_stub(monkeypatch, ciks=[], known=[], fr_count=0)
    result = monitor_mod.check_once(object(), FakeSettings(), fetch=lambda url: {"count": 0}, today="2026-09-24")
    assert set(result["unresolved"]) == set(monitor_mod.FILERS)
    assert result["pending_filings"] == []


def test_check_once_never_lists_an_accession_the_graph_already_has(monkeypatch):
    _driver_stub(monkeypatch, ciks=[{"ticker": "NVDA", "cik": 1045810}],
                known=[{"accession_no": "0001045810-26-000002"}], fr_count=0)

    def fetch(url):
        if "submissions" in url:
            return _submissions(["10-K"], ["2026-02-20"], ["0001045810-26-000002"])
        return {"count": 0}

    result = monitor_mod.check_once(object(), FakeSettings(), fetch=fetch, today="2026-09-24")
    assert result["pending_count"] == 0


def test_check_once_new_since_is_never_negative(monkeypatch):
    _driver_stub(monkeypatch, ciks=[], known=[], fr_count=50)
    result = monitor_mod.check_once(object(), FakeSettings(), fetch=lambda url: {"count": 3}, today="2026-09-24")
    assert result["federal_register"]["new_since"] == 0


def test_check_once_reports_the_snapshots_own_as_of_separately_from_the_check_date(monkeypatch):
    # The graph was built on 2026-09-20; the check itself runs with as_of=2026-09-24 (the comparison horizon).
    # The two must never be conflated — see monitor.py's SNAPSHOT_QUERY comment.
    _driver_stub(monkeypatch, ciks=[], known=[], fr_count=0,
                snapshot={"id": "snap-20260920-aaaaaaaaaa", "as_of": "2026-09-20"})
    result = monitor_mod.check_once(object(), FakeSettings(), fetch=lambda url: {"count": 0}, today="2026-09-24")
    assert result["as_of"] == "2026-09-24"
    assert result["snapshot_as_of"] == "2026-09-20"
    assert result["snapshot_id"] == "snap-20260920-aaaaaaaaaa"


def test_check_once_snapshot_fields_are_none_without_a_snapshot_node(monkeypatch):
    _driver_stub(monkeypatch, ciks=[], known=[], fr_count=0)   # no snapshot= given: SNAPSHOT_QUERY returns []
    result = monitor_mod.check_once(object(), FakeSettings(), fetch=lambda url: {"count": 0}, today="2026-09-24")
    assert result["snapshot_id"] is None and result["snapshot_as_of"] is None


# ---------------------------------------------------------------------- check_once: recorded fixture, fixed pending list

def _fixture_fetch():
    """A URL -> parsed-JSON dispatcher over the recorded fixtures in tests/fixtures/freshness/ (the REAL SEC
    submissions shape: parallel arrays under filings.recent, a non-empty filings.files, Form 4 / 8-K / 6-K noise, an
    in-window amendment for each filer, and a pre-ANNUAL_SINCE annual that must never show up as pending)."""
    files = {"CIK0001045810.json": json.loads((FIXTURES / "CIK0001045810.json").read_text(encoding="utf-8")),
            "CIK0001046179.json": json.loads((FIXTURES / "CIK0001046179.json").read_text(encoding="utf-8"))}
    fr_count = json.loads((FIXTURES / "fr_count.json").read_text(encoding="utf-8"))

    def fetch(url: str) -> dict:
        for name, payload in files.items():
            if name.removesuffix(".json") in url:
                return payload
        return fr_count   # the Federal Register count-query URL

    return fetch


def test_check_once_with_recorded_submissions_json_and_a_recorded_fr_count_reproduces_a_fixed_pending_list(monkeypatch):
    # NVDA's base 10-K is already "known" (in the SERVED graph); its 10-K/A amendment and 10-Q are not. TSM has
    # nothing known at all yet. Every other filer in FILERS is unresolved (no cik in this fixture's graph state).
    _driver_stub(monkeypatch, ciks=[{"ticker": "NVDA", "cik": 1045810}, {"ticker": "TSM", "cik": 1046179}],
                known=[{"accession_no": "0001045810-26-000001"}], fr_count=35)

    result = monitor_mod.check_once(object(), FakeSettings(), fetch=_fixture_fetch(), today="2026-09-24")

    pending_by_accession = {r["accession_no"]: r for r in result["pending_filings"]}
    assert set(pending_by_accession) == {"0001045810-26-000002", "0001045810-26-000010",
                                         "0001046179-26-000001", "0001046179-26-000002"}
    assert pending_by_accession["0001045810-26-000002"]["form"] == "10-K/A"   # the in-window amendment
    assert pending_by_accession["0001045810-26-000010"]["form"] == "10-Q"
    assert pending_by_accession["0001046179-26-000001"]["form"] == "20-F"
    assert pending_by_accession["0001046179-26-000002"]["form"] == "20-F/A"
    # The already-known base 10-K, the pre-ANNUAL_SINCE 2022 annuals, and every noise form (8-K, Form 4, 6-K,
    # 10-Q/A — quarterlies are matched on the EXACT form, never their own amendments) must never appear.
    never_pending = {"0001045810-26-000001", "0001045810-22-000001", "0001045810-26-000005",
                     "0001045810-26-000003", "0001045810-26-000011", "0001046179-22-000001",
                     "0001046179-26-000005"}
    assert not (never_pending & set(pending_by_accession))
    assert set(result["unresolved"]) == set(monitor_mod.FILERS) - {"NVDA", "TSM"}
    assert result["federal_register"] == {"graph_count": 35, "live_count": 40, "new_since": 5}


# ---------------------------------------------------------------------- lease

def test_lease_query_sets_the_lock_before_filtering_to_avoid_the_lost_update_race():
    q = monitor_mod.LEASE_QUERY
    assert q.index("SET l._lock = true") < q.index("WHERE l.until IS NULL")


def test_acquire_lease_maps_the_ok_column(monkeypatch):
    monkeypatch.setattr(monitor_mod, "run_cypher", lambda d, q, **p: [{"ok": True}])
    assert monitor_mod._acquire_lease(object(), "m1") is True
    monkeypatch.setattr(monitor_mod, "run_cypher", lambda d, q, **p: [{"ok": False}])
    assert monitor_mod._acquire_lease(object(), "m1") is False
    monkeypatch.setattr(monitor_mod, "run_cypher", lambda d, q, **p: [])
    assert monitor_mod._acquire_lease(object(), "m1") is False


# ---------------------------------------------------------------------- persistence round trip

def test_load_persisted_is_none_with_no_stored_row(monkeypatch):
    monkeypatch.setattr(monitor_mod, "run_cypher", lambda d, q, **p: [{"checked_at": None}])
    assert monitor_mod._load_persisted(object()) is None


def test_persist_then_load_round_trips_the_nested_json_fields(monkeypatch):
    store: dict = {}

    def fake_run_cypher(driver, query, **params):
        if query == monitor_mod.PUT_FRESHNESS_QUERY:
            store.update(params)
            return []
        if query == monitor_mod.GET_FRESHNESS_QUERY:
            return [dict(store)] if store else [{"checked_at": None}]
        raise AssertionError(query)

    monkeypatch.setattr(monitor_mod, "run_cypher", fake_run_cypher)
    result = {"checked_at": "2026-09-24T00:00:00+00:00", "as_of": "2026-09-24", "status": "ok", "error": None,
             "pending_filings": [{"ticker": "NVDA"}],
             "federal_register": {"graph_count": 1, "live_count": 2, "new_since": 1},
             "unresolved": [], "duration_s": 1.23, "pending_count": 1}
    monitor_mod._persist(object(), result)
    loaded = monitor_mod._load_persisted(object())
    assert loaded["pending_filings"] == [{"ticker": "NVDA"}]
    assert loaded["federal_register"]["live_count"] == 2
    assert loaded["status"] == "ok"


def test_persist_never_raises_when_the_write_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(monitor_mod, "run_cypher", boom)
    monitor_mod._persist(object(), {"checked_at": "x"})  # must not raise


def test_load_persisted_keeps_an_error_only_row_with_no_good_check_yet(monkeypatch):
    """docs/v2/M4_PLAN.md 15.10: a persisted error must not be discarded as "no history" just because there has
    never been a good check — its own `last_error_at` says otherwise."""
    row = {"checked_at": None, "as_of": None, "snapshot_id": None, "snapshot_as_of": None, "status": "error",
          "error": "sec down", "last_error_at": "2026-09-24T00:00:00+00:00", "pending_json": None, "fr_json": None,
          "unresolved_json": None, "duration_s": None, "pending_count": None}
    monkeypatch.setattr(monitor_mod, "run_cypher", lambda d, q, **p: [row])
    loaded = monitor_mod._load_persisted(object())
    assert loaded is not None
    assert loaded["status"] == "error" and loaded["last_error_at"] == "2026-09-24T00:00:00+00:00"
    assert loaded["checked_at"] is None


# ---------------------------------------------------------------------- _error_result (findings 21 + 25)

def test_error_result_keeps_every_field_of_the_last_good_result():
    previous = {"checked_at": "2026-01-01T00:00:00+00:00", "as_of": "2026-01-01", "snapshot_id": "s1",
               "snapshot_as_of": "2025-12-31", "status": "ok", "error": None, "last_error_at": None,
               "pending_count": 3, "pending_filings": [{"ticker": "NVDA"}],
               "federal_register": {"graph_count": 1, "live_count": 2, "new_since": 1}, "unresolved": [],
               "duration_s": 1.5}
    result = monitor_mod._error_result(previous, RuntimeError("sec down"))
    assert result["checked_at"] == previous["checked_at"]
    assert result["pending_count"] == 3 and result["pending_filings"] == previous["pending_filings"]
    assert result["federal_register"] == previous["federal_register"]
    assert result["snapshot_as_of"] == previous["snapshot_as_of"] and result["as_of"] == previous["as_of"]
    assert result["status"] == "error" and result["error"] == "sec down"
    assert result["last_error_at"] is not None


def test_error_result_with_no_previous_good_state_is_an_all_empty_shape_plus_the_error():
    result = monitor_mod._error_result(None, RuntimeError("boom"))
    assert result["checked_at"] is None and result["pending_count"] == 0 and result["pending_filings"] == []
    assert result["federal_register"] is None
    assert result["status"] == "error" and result["error"] == "boom" and result["last_error_at"] is not None


def test_error_result_chains_through_a_second_failure_without_moving_checked_at():
    first = monitor_mod._error_result(None, RuntimeError("first"))
    time.sleep(0.01)
    second = monitor_mod._error_result(first, RuntimeError("second"))
    assert second["checked_at"] is None
    assert second["last_error_at"] != first["last_error_at"]
    assert second["error"] == "second"


def test_run_check_keeps_the_last_good_result_when_a_later_check_fails(monkeypatch):
    _driver_stub(monkeypatch)
    good_result = {"checked_at": datetime.now(UTC).isoformat(), "as_of": "2026-09-24", "pending_count": 5,
                  "pending_filings": [{"ticker": "NVDA"}],
                  "federal_register": {"graph_count": 1, "live_count": 2, "new_since": 1}, "unresolved": [],
                  "duration_s": 0.1}
    _stub_check_once(monkeypatch, result=good_result)
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    m._busy.acquire()
    m._run_check()
    assert m._last["status"] == "ok" and m._last["pending_count"] == 5

    def boom(*a, **kw):
        raise RuntimeError("sec 403")

    monkeypatch.setattr(monitor_mod, "check_once", boom)
    m._busy.acquire()
    m._run_check()
    assert m._last["status"] == "error"
    assert m._last["pending_count"] == 5   # the last good count — never zeroed by the failed attempt
    assert m._last["pending_filings"] == [{"ticker": "NVDA"}]
    assert m._last["checked_at"] == good_result["checked_at"]   # never overwritten by a failed attempt
    assert m._last["last_error_at"] is not None
    assert m.status_payload()["last_error_at"] == m._last["last_error_at"]


# ---------------------------------------------------------------------- error retry backoff (findings 21 + 25)

def test_is_stale_or_missing_false_soon_after_an_error_within_the_backoff_window():
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    m._last = {**_stub_result(), "status": "error",
              "last_error_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()}
    assert m._is_stale_or_missing() is False


def test_is_stale_or_missing_true_once_the_error_backoff_window_has_passed():
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    m._last = {**_stub_result(), "status": "error",
              "last_error_at": (datetime.now(UTC) -
                                timedelta(minutes=monitor_mod.ERROR_RETRY_MINUTES + 1)).isoformat()}
    assert m._is_stale_or_missing() is True


def test_next_wait_seconds_is_the_retry_backoff_after_an_error():
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    m._last = {**_stub_result(), "status": "error", "last_error_at": datetime.now(UTC).isoformat()}
    assert m._next_wait_seconds() == pytest.approx(monitor_mod.ERROR_RETRY_MINUTES * 60, abs=2)


def test_next_wait_seconds_is_the_poll_interval_after_an_ok_check():
    settings = FakeSettings()
    settings.freshness_poll_hours = 6
    m = monitor_mod.FreshnessMonitor(object(), settings)
    m._last = {**_stub_result(), "checked_at": datetime.now(UTC).isoformat(), "status": "ok"}
    assert m._next_wait_seconds() == pytest.approx(6 * 3600, abs=2)


def test_next_wait_seconds_anchors_on_checked_at_not_on_now_at_boot():
    """The bug the loop used to have: a monitor that boots with a persisted OK check already 5 h into a 6 h poll
    interval must wait only the REMAINING 1 h, never a fresh full 6 h from boot time (which would silently push the
    real next check out to 11 h after the last one, and make next_check_at read as already-past for 5 h)."""
    settings = FakeSettings()
    settings.freshness_poll_hours = 6
    m = monitor_mod.FreshnessMonitor(object(), settings)
    m._last = {**_stub_result(), "checked_at": (datetime.now(UTC) - timedelta(hours=5)).isoformat(), "status": "ok"}
    assert m._next_wait_seconds() == pytest.approx(3600, abs=2)
    assert m._is_stale_or_missing() is False


def test_next_wait_seconds_is_negative_once_a_check_is_overdue():
    settings = FakeSettings()
    settings.freshness_poll_hours = 1
    m = monitor_mod.FreshnessMonitor(object(), settings)
    m._last = {**_stub_result(), "checked_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(), "status": "ok"}
    assert m._next_wait_seconds() < 0
    assert m._is_stale_or_missing() is True


# ---------------------------------------------------------------------- FreshnessMonitor lifecycle

def test_stop_returns_promptly_even_mid_sleep(monkeypatch):
    _driver_stub(monkeypatch)
    settings = FakeSettings()
    settings.freshness_boot_delay_s = 999  # the thread is inside this wait when stop() is called
    m = monitor_mod.FreshnessMonitor(object(), settings)
    m.start()
    time.sleep(0.05)
    started = time.monotonic()
    m.stop(timeout=5)
    assert time.monotonic() - started < 4.5


def test_stop_is_a_no_op_when_no_monitor_was_started():
    class App:
        class state:
            freshness_monitor = None

    monitor_mod.stop(App)  # must not raise


def test_check_now_returns_ok_and_marks_the_monitor_configured(monkeypatch):
    _driver_stub(monkeypatch)
    _stub_check_once(monkeypatch)
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    payload = m.check_now(timeout_s=5)
    assert payload["status"] == "ok"
    assert payload["configured"] is True
    assert m.checking is False


def test_check_now_raises_monitor_busy_when_a_check_is_already_running(monkeypatch):
    _driver_stub(monkeypatch)
    release = threading.Event()
    _stub_check_once(monkeypatch)
    monkeypatch.setattr(monitor_mod, "check_once",
                        lambda driver, settings, **kw: (release.wait(2), _stub_result())[1])
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    t = threading.Thread(target=m.check_now, kwargs={"timeout_s": 5})
    t.start()
    time.sleep(0.05)
    try:
        with pytest.raises(monitor_mod.MonitorBusy):
            m.check_now()
    finally:
        release.set()
        t.join(5)


def _stub_result():
    return {"checked_at": datetime.now(UTC).isoformat(), "as_of": "x", "pending_count": 0, "pending_filings": [],
            "federal_register": None, "unresolved": [], "duration_s": 0}


def test_check_now_reports_an_error_status_when_it_overruns_the_bound(monkeypatch):
    _driver_stub(monkeypatch)
    _stub_check_once(monkeypatch, delay=0.3)
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    payload = m.check_now(timeout_s=0.05)
    assert payload["status"] == "error"
    assert "did not finish" in payload["error"]
    time.sleep(0.5)  # let the orphaned background check finish so it releases the lock before the next test


def test_check_now_raises_monitor_busy_when_unconfigured():
    settings = FakeSettings()
    settings.sec_user_agent = ""
    m = monitor_mod.FreshnessMonitor(object(), settings)
    with pytest.raises(monitor_mod.MonitorBusy):
        m.check_now()


def test_try_check_skips_silently_when_the_lease_is_held_elsewhere(monkeypatch):
    _driver_stub(monkeypatch, lease_ok=False)
    called = []
    monkeypatch.setattr(monitor_mod, "check_once", lambda *a, **k: called.append(1))
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    m._try_check()
    assert called == []
    assert m.checking is False  # the busy lock must be released even when the lease is refused


def test_try_check_survives_a_lease_error_and_releases_the_busy_lock(monkeypatch):
    """A transient Neo4j hiccup on the lease call must never leak `_busy` or kill the daemon thread (it used to:
    the exception propagated straight out of `_try_check`)."""
    _driver_stub(monkeypatch, lease_error=RuntimeError("neo4j connection reset"))
    called = []
    monkeypatch.setattr(monitor_mod, "check_once", lambda *a, **k: called.append(1))
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    m._try_check()   # must not raise
    assert called == []
    assert m.checking is False


def test_try_check_still_runs_on_a_later_call_after_a_lease_error(monkeypatch):
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    _driver_stub(monkeypatch, lease_error=RuntimeError("neo4j connection reset"))
    m._try_check()
    assert m.checking is False

    _driver_stub(monkeypatch, lease_ok=True)
    _stub_check_once(monkeypatch)
    m._try_check()
    assert m._last is not None and m._last["status"] == "ok"
    assert m.checking is False


def test_safe_try_check_survives_an_unexpected_error_from_try_check_itself(monkeypatch):
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())

    def boom():
        raise RuntimeError("something nobody guarded against")

    monkeypatch.setattr(m, "_try_check", boom)
    m._safe_try_check()   # must not raise — the polling thread must survive


# ---------------------------------------------------------------------- status_payload / summary

def test_status_payload_reports_never_with_no_history(monkeypatch):
    _driver_stub(monkeypatch)
    settings = FakeSettings()
    settings.freshness_boot_delay_s = 300
    m = monitor_mod.FreshnessMonitor(object(), settings)
    payload = m.status_payload()
    expected_next_check = (m._started_at + timedelta(seconds=300)).isoformat()
    assert payload == {"configured": True, "enabled": True, "status": "never", "checked_at": None,
                       "snapshot_as_of": None, "last_error_at": None, "next_check_at": expected_next_check,
                       "pending_count": 0, "pending_filings": [], "federal_register": None, "unresolved": [],
                       "duration_s": None}


def test_status_payload_snapshot_as_of_is_the_graphs_data_date_not_the_check_date():
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    m._last = {**_stub_result(), "as_of": "2026-09-24", "snapshot_as_of": "2026-09-20", "status": "ok"}
    assert m.status_payload()["snapshot_as_of"] == "2026-09-20"


def test_status_payload_reports_stale_past_twice_the_poll_interval():
    settings = FakeSettings()
    settings.freshness_poll_hours = 1
    m = monitor_mod.FreshnessMonitor(object(), settings)
    m._last = {**_stub_result(), "checked_at": (datetime.now(UTC) - timedelta(hours=3)).isoformat(), "status": "ok"}
    assert m.status_payload()["status"] == "stale"


def test_status_payload_reports_ok_just_inside_the_stale_window():
    settings = FakeSettings()
    settings.freshness_poll_hours = 1
    m = monitor_mod.FreshnessMonitor(object(), settings)
    m._last = {**_stub_result(), "checked_at": (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
              "status": "ok"}
    assert m.status_payload()["status"] == "ok"


def test_status_payload_reports_error_when_the_last_check_failed():
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    m._last = {**_stub_result(), "status": "error", "error": "boom"}
    assert m.status_payload()["status"] == "error"


def test_status_payload_reports_unconfigured_even_with_a_recent_ok_check():
    settings = FakeSettings()
    settings.sec_user_agent = ""
    m = monitor_mod.FreshnessMonitor(object(), settings)
    m._last = {**_stub_result(), "status": "ok"}
    assert m.status_payload()["status"] == "unconfigured"


# ---------------------------------------------------------------------- next_check_at (finding: known item)

def test_status_payload_next_check_at_is_none_when_unconfigured():
    settings = FakeSettings()
    settings.sec_user_agent = ""
    m = monitor_mod.FreshnessMonitor(object(), settings)
    m._last = {**_stub_result(), "status": "ok"}
    assert m.status_payload()["next_check_at"] is None


def test_status_payload_next_check_at_after_an_ok_check_is_checked_at_plus_the_poll_interval():
    settings = FakeSettings()
    settings.freshness_poll_hours = 6
    m = monitor_mod.FreshnessMonitor(object(), settings)
    checked_at = datetime.now(UTC) - timedelta(hours=1)
    m._last = {**_stub_result(), "checked_at": checked_at.isoformat(), "status": "ok"}
    expected = (checked_at + timedelta(hours=6)).isoformat()
    assert m.status_payload()["next_check_at"] == expected


def test_status_payload_next_check_at_after_an_error_is_last_error_at_plus_the_retry_backoff():
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    last_error_at = datetime.now(UTC) - timedelta(minutes=5)
    m._last = {**_stub_result(), "status": "error", "error": "boom", "last_error_at": last_error_at.isoformat()}
    expected = (last_error_at + timedelta(minutes=monitor_mod.ERROR_RETRY_MINUTES)).isoformat()
    payload = m.status_payload()
    assert payload["next_check_at"] == expected
    assert payload["last_error_at"] == last_error_at.isoformat()


# ---------------------------------------------------------------------- summary (finding 18)

def test_summary_reports_never_before_any_state_is_loaded():
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    assert m.summary() == {"status": "never", "checked_at": None, "pending_count": 0}


def test_summary_reports_unconfigured_when_sec_user_agent_is_missing():
    settings = FakeSettings()
    settings.sec_user_agent = ""
    m = monitor_mod.FreshnessMonitor(object(), settings)
    assert m.summary() == {"status": "unconfigured", "checked_at": None, "pending_count": 0}


def test_summary_never_touches_the_database(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("summary() must be in-memory only — /api/stats calls it on the event loop")

    monkeypatch.setattr(monitor_mod, "run_cypher", boom)
    m = monitor_mod.FreshnessMonitor(object(), FakeSettings())
    m._last = {**_stub_result(), "status": "ok", "pending_count": 2}
    assert m.summary() == {"status": "ok", "checked_at": m._last["checked_at"], "pending_count": 2}


# ---------------------------------------------------------------------- start_if_enabled / stop wiring

def test_start_if_enabled_leaves_the_monitor_none_when_disabled():
    settings = FakeSettings()
    settings.freshness_enabled = False

    class App:
        class state:
            pass

    App.state.settings = settings
    monitor_mod.start_if_enabled(App)
    assert App.state.freshness_monitor is None


def test_start_if_enabled_starts_a_monitor_when_enabled(monkeypatch):
    _driver_stub(monkeypatch)
    settings = FakeSettings()
    settings.freshness_boot_delay_s = 999

    class App:
        class state:
            pass

    App.state.settings = settings
    App.state.driver = object()
    monitor_mod.start_if_enabled(App)
    try:
        assert isinstance(App.state.freshness_monitor, monitor_mod.FreshnessMonitor)
    finally:
        monitor_mod.stop(App)

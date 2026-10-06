"""The maintenance thread (kill-level refresh, lease renewal, sweep) and the kill-switch CLI: no server needed.

The scheduler is driven by ``tick()`` with an injected clock wherever timing matters, so no test sleeps for an interval;
the few tests that run the real thread use tiny intervals and bounded waits.
"""

import importlib.util
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_state_inprocess import FakeClock, FakeStoreDriver, ask, build_inprocess, state_settings

from semigraph.serve.state import StateUnavailable, maintenance, settle_queue
from semigraph.serve.state.backend import LeaseRegistry
from semigraph.serve.state.maintenance import MaintenanceThread

ROOT = Path(__file__).resolve().parents[1]


class FakeBackend:
    """Records what the thread asks of a backend; ``fail`` names calls that raise StateUnavailable."""

    def __init__(self):
        self.registry = LeaseRegistry()
        self.calls: list[tuple] = []
        self.fail: set[str] = set()
        self.renewed: dict[str, bool] = {}
        self.pending = 0
        self.flush_threads: list[str] = []

    def refresh_kill_level(self):
        self.calls.append(("kill",))
        if "kill" in self.fail:
            raise StateUnavailable("down")
        return "off"

    def renew(self, lease_id, now_wall):
        self.calls.append(("renew", lease_id, now_wall))
        if f"renew:{lease_id}" in self.fail:
            raise StateUnavailable("down")
        return self.renewed.get(lease_id, True)

    def sweep(self, now):
        self.calls.append(("sweep", now))
        if "sweep" in self.fail:
            raise StateUnavailable("down")
        return 0

    def pending_settles(self):
        return self.pending

    def drain_settles(self):
        self.calls.append(("drain",))
        if "drain" in self.fail:
            raise StateUnavailable("down")
        return 0

    def flush_settles(self, budget_s):
        self.calls.append(("flush", budget_s))
        self.flush_threads.append(threading.current_thread().name)
        if "flush" in self.fail:
            raise StateUnavailable("down")
        return 0

    def kinds(self):
        return [call[0] for call in self.calls]


def thread_for(backend, clock=None, **settings):
    clock = clock or FakeClock()
    values = SimpleNamespace(kill_switch_refresh_s=10.0, lease_renew_s=15.0, **settings)
    return MaintenanceThread(backend, values, clock=clock.mono, wall=clock.wall), clock


# ------------------------------------------------------------------------------------------------ the first read

def test_start_reads_the_kill_level_before_it_returns_so_serving_never_begins_with_an_unread_level():
    backend = FakeBackend()
    thread, _ = thread_for(backend)
    thread.start()
    try:
        assert backend.calls[0] == ("kill",)            # already done when start() returned, whatever the thread did
    finally:
        thread.stop()


def test_a_failed_first_read_is_logged_and_does_not_stop_the_thread_from_starting(caplog):
    backend = FakeBackend()
    backend.fail.add("kill")
    thread, _ = thread_for(backend)
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        thread.start()
    try:
        assert thread.is_alive()
        assert any("state_maintenance_failed" in record.message and "kill_refresh" in record.message
                   for record in caplog.records)
    finally:
        thread.stop()


def test_the_first_read_with_the_real_backend_opens_the_gate_for_serving():
    backend, *_ = build_inprocess()
    backend._forget_kill_level()                                    # noqa: SLF001 - what a boot does
    assert backend.kill_level() == "on"
    thread = MaintenanceThread(backend, state_settings())
    thread.start()
    try:
        assert backend.kill_level() == "off"
    finally:
        thread.stop()


# ----------------------------------------------------------------------------------------------- the schedule

def test_the_kill_level_is_refreshed_every_ten_seconds_and_leases_every_fifteen():
    backend = FakeBackend()
    thread, clock = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    backend.registry.add("lease-a")
    for _ in range(46):                                                           # a tick every second for 46 s
        clock.advance(1)
        thread.tick()
    kills = [call for call in backend.calls if call[0] == "kill"]
    renews = [call for call in backend.calls if call[0] == "renew"]
    sweeps = [call for call in backend.calls if call[0] == "sweep"]
    assert len(kills) == 1 + 4                                                    # the first read, then t=10,20,30,40
    assert len(renews) == 3 and len(sweeps) == 3                                  # t=15,30,45


def test_tick_returns_the_seconds_until_the_next_task_is_due():
    backend = FakeBackend()
    thread, clock = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    assert thread.tick() == pytest.approx(10.0)
    clock.advance(4)
    assert thread.tick() == pytest.approx(6.0)
    clock.advance(6)                                                              # t=10: the kill refresh runs
    assert thread.tick() == pytest.approx(5.0)                                    # the leases are due at t=15
    clock.advance(5)
    assert thread.tick() == pytest.approx(5.0)                                    # next kill at t=20


def test_a_long_stall_runs_each_task_once_and_does_not_burst():
    backend = FakeBackend()
    thread, clock = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    clock.advance(1000)
    thread.tick()
    assert backend.kinds().count("kill") == 2 and backend.kinds().count("sweep") == 1


def test_every_registered_lease_is_renewed_with_the_wall_clock_and_the_sweep_uses_it_too():
    backend = FakeBackend()
    thread, clock = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    for lease_id in ("a", "b"):
        backend.registry.add(lease_id)
    clock.advance(15)
    thread.tick()
    assert ("renew", "a", clock.wall()) in backend.calls and ("renew", "b", clock.wall()) in backend.calls
    assert ("sweep", clock.wall()) in backend.calls


def test_a_lease_the_backend_no_longer_knows_leaves_the_registry():
    backend = FakeBackend()
    backend.renewed["gone"] = False
    thread, clock = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    backend.registry.add("gone")
    backend.registry.add("alive")
    clock.advance(15)
    thread.tick()
    assert backend.registry.active() == ["alive"]


# --------------------------------------------------------------------------------- an error never kills it

def test_one_failing_task_does_not_stop_the_others_and_is_retried_at_the_next_tick(caplog):
    backend = FakeBackend()
    thread, clock = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    backend.registry.add("a")
    backend.registry.add("b")
    backend.fail |= {"kill", "renew:a", "sweep"}
    clock.advance(30)
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        thread.tick()
    assert ("renew", "b", clock.wall()) in backend.calls              # b was renewed although a failed
    assert "sweep" in backend.kinds()                                   # the sweep ran although renewal failed
    failed = [r.message for r in caplog.records if "state_maintenance_failed" in r.message]
    assert all(any(task in message for message in failed) for task in ("kill_refresh", "renew", "sweep"))
    backend.fail.clear()
    backend.calls.clear()
    clock.advance(30)
    thread.tick()
    assert {"kill", "renew", "sweep"} <= set(backend.kinds())            # everything retried


def test_an_unexpected_exception_type_is_logged_not_raised():
    backend = FakeBackend()

    def boom(now_wall=None):
        raise RuntimeError("a bug in a task")

    backend.refresh_kill_level = boom
    thread, clock = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    clock.advance(10)
    thread.tick()                                                                 # must not raise


def test_the_real_thread_survives_a_failing_task_and_keeps_ticking():
    backend = FakeBackend()
    backend.fail.add("kill")
    thread = MaintenanceThread(backend, SimpleNamespace(kill_switch_refresh_s=0.02, lease_renew_s=0.02))
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while backend.kinds().count("kill") < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend.kinds().count("kill") >= 4 and thread.is_alive()
    finally:
        assert thread.stop() is True


# -------------------------------------------------------------------------------- the settle retry queue

def test_every_tick_retries_the_queued_settles_even_when_no_other_task_is_due():
    backend = FakeBackend()
    thread, _ = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    thread.tick()
    thread.tick()
    assert backend.kinds().count("drain") == 2 and backend.kinds().count("sweep") == 0


def test_the_wait_is_capped_at_the_retry_interval_while_settles_are_pending_and_not_otherwise():
    backend = FakeBackend()
    thread, _ = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    assert thread.tick() == pytest.approx(10.0)
    backend.pending = 3
    assert thread.tick() == pytest.approx(settle_queue.RETRY_INTERVAL_S)


def test_the_drain_runs_after_the_due_tasks_so_a_slow_database_never_delays_the_kill_refresh():
    backend = FakeBackend()
    thread, clock = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    backend.calls.clear()
    clock.advance(30)
    thread.tick()
    kinds = backend.kinds()
    assert kinds.index("kill") < kinds.index("sweep") < kinds.index("drain")


def test_a_failing_drain_is_logged_and_the_other_tasks_still_ran(caplog):
    backend = FakeBackend()
    backend.fail.add("drain")
    thread, clock = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    clock.advance(30)
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        thread.tick()
    assert {"kill", "sweep", "drain"} <= set(backend.kinds())
    assert any("state_maintenance_failed" in r.message and "settle_drain" in r.message for r in caplog.records)


class BareBackend:
    """A backend written before the retry queue existed: only the three calls the thread always made."""

    def __init__(self):
        self.registry = LeaseRegistry()
        self.calls: list[str] = []

    def refresh_kill_level(self):
        self.calls.append("kill")

    def renew(self, lease_id, now_wall):
        return True

    def sweep(self, now):
        self.calls.append("sweep")
        return 0


def test_a_backend_without_a_retry_queue_is_still_served():
    backend = BareBackend()
    thread, clock = thread_for(backend)
    thread._first_read()                                                          # noqa: SLF001
    clock.advance(30)
    assert thread.tick() >= 0.0 and thread.stop() is True
    assert backend.calls == ["kill", "kill", "sweep"]


def test_stop_flushes_the_queued_settles_once_after_the_thread_has_ended_within_the_budget():
    backend = FakeBackend()
    thread = MaintenanceThread(backend, SimpleNamespace(kill_switch_refresh_s=30.0, lease_renew_s=30.0))
    thread.start()
    assert thread.stop() is True
    assert [call for call in backend.calls if call[0] == "flush"] == [("flush", maintenance.FLUSH_BUDGET_S)]
    assert "state-maintenance" not in backend.flush_threads and not thread.is_alive()   # flushed by the caller of stop
    assert thread.stop() is True and backend.kinds().count("flush") == 1                # a second stop flushes nothing


def test_a_failing_flush_is_logged_and_stop_still_reports_the_thread_ended(caplog):
    backend = FakeBackend()
    backend.fail.add("flush")
    thread = MaintenanceThread(backend, SimpleNamespace(kill_switch_refresh_s=30.0, lease_renew_s=30.0))
    thread.start()
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        assert thread.stop() is True
    assert any("state_maintenance_failed" in r.message and "settle_flush" in r.message for r in caplog.records)


def test_the_thread_retries_a_queued_settle_on_the_real_backend_and_stop_flushes_what_is_left():
    backend, fake, _, clock = build_inprocess(state_settings(max_concurrent_answers=5, paid_per_ip_per_day=0))
    first, second = ask(backend, clock, ip="a"), ask(backend, clock, ip="b")
    fake.fail_settle = 2
    backend.reconcile(first.lease_id, outcome="done", usage=None, cost_micro=1)
    backend.reconcile(second.lease_id, outcome="done", usage=None, cost_micro=1)
    assert backend.pending_settles() == 2
    thread = MaintenanceThread(backend, state_settings(), clock=clock.mono, wall=clock.wall)
    thread._first_read()                                                          # noqa: SLF001
    thread.tick()
    assert backend.pending_settles() == 0
    assert {fake.rows[lease.lease_id]["status"] for lease in (first, second)} == {"settled"}
    third = ask(backend, clock, ip="c")
    fake.fail_settle = 1
    backend.reconcile(third.lease_id, outcome="done", usage=None, cost_micro=1)
    assert backend.pending_settles() == 1
    assert thread.stop() is True and backend.pending_settles() == 0           # the shutdown flush wrote it
    assert fake.rows[third.lease_id]["status"] == "settled"


# ------------------------------------------------------------------------------------------ stop and the rest

def test_the_thread_is_a_daemon_and_stop_joins_it_within_a_bound():
    backend = FakeBackend()
    thread = MaintenanceThread(backend, SimpleNamespace(kill_switch_refresh_s=30.0, lease_renew_s=30.0))
    thread.start()
    assert thread.daemon is True
    started = time.monotonic()
    assert thread.stop(timeout=2.0) is True
    assert time.monotonic() - started < 1.0 and not thread.is_alive()
    assert thread.stop() is True                                              # idempotent


def test_stop_before_start_is_harmless():
    thread, _ = thread_for(FakeBackend())
    assert thread.stop() is True


def test_stop_reports_a_thread_that_did_not_finish_within_the_bound(caplog):
    release = threading.Event()
    backend = FakeBackend()
    backend.sweep = lambda now: release.wait(5) or 0                          # a task stuck in a slow database call
    thread = MaintenanceThread(backend, SimpleNamespace(kill_switch_refresh_s=0.01, lease_renew_s=0.01))
    thread.start()
    try:
        time.sleep(0.1)
        with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
            assert thread.stop(timeout=0.1) is False
        assert any("did not stop" in record.message for record in caplog.records)
    finally:
        release.set()
        assert thread.stop(timeout=3.0) is True


@pytest.mark.parametrize("field", ["kill_switch_refresh_s", "lease_renew_s"])
@pytest.mark.parametrize("bad", [0, -1, None])
def test_a_non_positive_interval_is_refused(field, bad):
    values = SimpleNamespace(kill_switch_refresh_s=10.0, lease_renew_s=15.0)
    setattr(values, field, bad)
    with pytest.raises(ValueError, match=field):
        MaintenanceThread(FakeBackend(), values)


def test_a_missing_setting_is_named():
    with pytest.raises(ValueError, match="lease_renew_s"):
        MaintenanceThread(FakeBackend(), SimpleNamespace(kill_switch_refresh_s=10.0))


# ----------------------------------------------------------------------------------- with the real backend

def test_the_thread_renews_started_leases_and_sweeps_abandoned_ones_on_the_real_backend():
    backend, fake, _, clock = build_inprocess(state_settings(max_concurrent_answers=5))
    started, abandoned = ask(backend, clock, ip="a"), ask(backend, clock, ip="b", estimate=70_000)
    backend.mark_started(started.lease_id)
    thread = MaintenanceThread(backend, state_settings(), clock=clock.mono, wall=clock.wall)
    thread._first_read()                                                          # noqa: SLF001
    for _ in range(9):                                                            # 90 s, a tick every 10 s
        clock.advance(10)
        thread.tick()
        backend.refresh_kill_level()                                              # (the tick did it at 10 s intervals)
    assert fake.rows[started.lease_id]["status"] == "reserved"
    assert fake.rows[started.lease_id]["lease_until"] > clock.wall()              # renewed again and again
    assert fake.rows[abandoned.lease_id]["outcome"] == "abandoned"                # swept after its 60 s ttl
    assert fake.rows[abandoned.lease_id]["cost_micro"] == 70_000
    assert backend.snapshot()["inflight"] == 1


# ------------------------------------------------------------------------------------------- the CLI

def load_cli():
    spec = importlib.util.spec_from_file_location("kill_switch_cli", ROOT / "scripts" / "kill_switch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cli_reads_a_level_through_the_policy_node(monkeypatch, capsys):
    cli, store = load_cli(), FakeStoreDriver()
    monkeypatch.setattr(cli, "open_driver", lambda args: store)
    store.policy["kill_switch"] = "retrieval_only"
    assert cli.main(["--direct", "get"]) == 0
    assert capsys.readouterr().out.strip() == "kill switch: retrieval_only"


def test_the_cli_status_is_an_alias_of_get_and_an_unset_policy_reads_off(monkeypatch, capsys):
    cli, store = load_cli(), FakeStoreDriver()
    monkeypatch.setattr(cli, "open_driver", lambda args: store)
    assert cli.main(["--direct", "status"]) == 0
    assert capsys.readouterr().out.strip() == "kill switch: off"


@pytest.mark.parametrize("level", ["on", "off"])
def test_the_cli_sets_the_two_levels_the_live_image_understands_and_reads_each_back(monkeypatch, capsys, level):
    cli, store = load_cli(), FakeStoreDriver()
    monkeypatch.setattr(cli, "open_driver", lambda args: store)
    assert cli.main(["--direct", level]) == 0
    assert store.policy["kill_switch"] == level
    assert f"kill switch: {level}" in capsys.readouterr().out


KNOW_FLAG = "--i-know-the-live-image-treats-it-as-off"


def test_the_cli_refuses_to_store_retrieval_only_directly_and_says_why_without_touching_the_database(monkeypatch):
    """The image that is live today reads only ``on`` as stopped, so a stored ``retrieval_only`` would let paid
    questions through."""
    cli = load_cli()
    monkeypatch.setattr(cli, "open_driver", lambda args: pytest.fail("the driver must not be opened on a refusal"))
    with pytest.raises(SystemExit) as refusal:
        cli.main(["--direct", "retrieval_only"])
    message = str(refusal.value)
    assert "retrieval_only" in message and "off" in message and KNOW_FLAG in message


def test_the_cli_stores_retrieval_only_directly_only_when_the_flag_says_the_operator_knows(monkeypatch, capsys):
    cli, store = load_cli(), FakeStoreDriver()
    monkeypatch.setattr(cli, "open_driver", lambda args: store)
    assert cli.main(["--direct", "retrieval_only", KNOW_FLAG]) == 0
    assert store.policy["kill_switch"] == "retrieval_only"
    assert "kill switch: retrieval_only" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["get", "status"])
def test_the_cli_reads_any_stored_level_directly_without_the_flag(monkeypatch, capsys, command):
    cli, store = load_cli(), FakeStoreDriver()
    monkeypatch.setattr(cli, "open_driver", lambda args: store)
    store.policy["kill_switch"] = "retrieval_only"
    assert cli.main(["--direct", command]) == 0
    assert "kill switch: retrieval_only" in capsys.readouterr().out


def test_the_cli_over_http_does_not_need_the_flag(monkeypatch, tmp_path):
    """Over HTTP the app, not this script, decides what a level means (the endpoint answers 422 until it is widened)."""
    cli = load_cli()
    sent = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"kill_switch": "retrieval_only", "ledger": {}}

    class Client:
        def __init__(self, **kwargs):
            pass

        def post(self, path, json):
            sent.append(json)
            return Response()

        def get(self, path):
            return Response()

    monkeypatch.setattr(cli.httpx, "Client", Client)
    env = tmp_path / "env"
    env.write_text("APP_BASE_URL=https://example.invalid\nADMIN_TOKEN=tok\n", encoding="utf-8")
    assert cli.main(["retrieval_only", "--env", str(env)]) == 0 and sent == [{"kill_switch": "retrieval_only"}]


def test_the_cli_refuses_an_unknown_level():
    cli = load_cli()
    with pytest.raises(SystemExit):
        cli.main(["--direct", "paused"])


def test_the_cli_prints_no_secret(monkeypatch, capsys):
    cli, store = load_cli(), FakeStoreDriver()
    monkeypatch.setattr(cli, "open_driver", lambda args: store)
    monkeypatch.setenv("NEO4J_PASSWORD", "hunter2-never-printed")  # gitleaks:allow
    monkeypatch.setenv("ADMIN_TOKEN", "token-never-printed")  # gitleaks:allow
    cli.main(["--direct", "on"])
    out = capsys.readouterr()
    assert "hunter2" not in out.out + out.err and "token-never" not in out.out + out.err


def test_the_cli_over_http_keeps_the_ops_script_contract(monkeypatch, capsys, tmp_path):
    """``scripts/ops.ps1`` calls ``on|off|status`` over the admin endpoint (Neo4j is private on Fly): unchanged."""
    cli = load_cli()
    sent = []

    class FakeResponse:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def post(self, path, json):
            sent.append((path, json))
            return FakeResponse({"kill_switch": "on" if json["kill_switch"] is True else json["kill_switch"]})

        def get(self, path):
            return FakeResponse({"kill_switch": "on", "ledger": {"today": {"paid": 1}}})

    monkeypatch.setattr(cli.httpx, "Client", FakeClient)
    env = tmp_path / "env"
    env.write_text("APP_BASE_URL=https://example.invalid\nADMIN_TOKEN=tok\n", encoding="utf-8")
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert cli.main(["on", "--env", str(env)]) == 0
    assert sent == [("/api/admin/policy", {"kill_switch": True})]            # a bool, as the pre-M5 endpoint wants
    assert cli.main(["retrieval_only", "--env", str(env)]) == 0
    assert sent[-1] == ("/api/admin/policy", {"kill_switch": "retrieval_only"})
    assert "tok" not in capsys.readouterr().out

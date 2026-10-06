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

from semigraph.serve.state import StateUnavailable
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


@pytest.mark.parametrize("level", ["on", "retrieval_only", "off"])
def test_the_cli_sets_each_of_the_three_levels_and_reads_it_back(monkeypatch, capsys, level):
    cli, store = load_cli(), FakeStoreDriver()
    monkeypatch.setattr(cli, "open_driver", lambda args: store)
    assert cli.main(["--direct", level]) == 0
    assert store.policy["kill_switch"] == level
    assert f"kill switch: {level}" in capsys.readouterr().out


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

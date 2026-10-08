"""The shared core's side of the per-address spend share (Wave 2 step 0; docs/v2/research/m5-councils/council4/verdict.md).

What the two backends share and this step adds, tested on ``StateCore`` itself (no backend, no ledger, no server):

* ``Denied.IP_SPEND`` / ``Denied.IP_SPEND_INFLIGHT`` and their wire values (the neo4j backend keys its reason strings on
  them);
* ``StateConfig.paid_spend_share_micro``: read with ``_need`` (a settings object that forgot it is refused, never read as
  "off"), whole micro-dollars, 0 = off;
* ``_charge_micro``: the estimate is kept ONLY when the cost is unknown (None); an integer cost on an ``abandoned`` ask is
  charged as given (the metered charge of an ask whose client went away, 0 when no paid call started);
* ``RebuildReport.per_ip_spend`` and the boot passthrough from the ledger's sums;
* ``_note_level`` / ``_note_pause``: one warning per day at half the count or the spend cap, and one at each pause.

The backends' own share accounting is tested with the backends (``test_state_contract``)."""

import logging
import threading
from types import SimpleNamespace

import pytest
from test_state_inprocess import FakeStoreDriver, state_settings

from semigraph.serve.state import Denied, RebuildReport, StateConfig, StateDrivers
from semigraph.serve.state.backend import StateCore

LOGGER = "semigraph.serve.state"
DAY, NEXT_DAY = "2026-10-08", "2026-10-09"
MAX_COUNT, MAX_SPEND_MICRO = 150, 10_000_000


def core(**settings) -> StateCore:
    return StateCore(StateConfig.from_settings(state_settings(**settings)), StateDrivers(state=FakeStoreDriver()), None)


def warnings_of(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


# --------------------------------------------------------------------------------------------- the new denials

def test_the_two_share_denials_have_their_own_wire_values_and_are_not_the_existing_ones():
    assert (Denied.IP_SPEND.value, Denied.IP_SPEND_INFLIGHT.value) == ("ip_spend", "ip_spend_inflight")
    assert len({d.value for d in Denied}) == len(Denied)


# ------------------------------------------------------------------------------------------------ configuration

def test_the_share_is_converted_to_whole_micro_dollars():
    # 1.32 has no exact binary form: the conversion reads its decimal form (usd_to_micro), so it is exactly 1_320_000.
    assert StateConfig.from_settings(state_settings(paid_spend_share_per_ip_usd=1.32)).paid_spend_share_micro == 1_320_000
    assert StateConfig.from_settings(state_settings(paid_spend_share_per_ip_usd=1.25)).paid_spend_share_micro == 1_250_000
    assert StateConfig.from_settings(state_settings(paid_spend_share_per_ip_usd=0.000001)).paid_spend_share_micro == 1


def test_the_production_share_is_stored_as_the_whole_micro_dollars_it_names():
    from semigraph.config import PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD
    share = StateConfig.from_settings(state_settings(paid_spend_share_per_ip_usd=PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD))
    assert share.paid_spend_share_micro == 1_320_000


def test_a_zero_share_is_off_and_a_missing_or_negative_one_is_refused():
    assert StateConfig.from_settings(state_settings(paid_spend_share_per_ip_usd=0)).paid_spend_share_micro == 0
    settings = state_settings()
    del settings.paid_spend_share_per_ip_usd
    with pytest.raises(ValueError, match="settings.paid_spend_share_per_ip_usd"):
        StateConfig.from_settings(settings)
    with pytest.raises(ValueError):
        StateConfig.from_settings(state_settings(paid_spend_share_per_ip_usd=-0.5))


# ------------------------------------------------------------------------------------------------ the charge

@pytest.mark.parametrize("outcome, cost, charged", [
    ("abandoned", None, None),          # unknown: the estimate stays
    ("abandoned", 0, 0),                # no paid call started: nothing is charged
    ("abandoned", 5, 5),                # the metered charge of an abandoned ask
    ("abandoned", -5, 0),               # floored, as for any outcome
    ("done", None, None),
    ("done", 0, 0),
    ("done", 1_500, 1_500),
    ("error", 90_000, 90_000),
    ("done", -5, 0),
])
def test_an_integer_cost_is_charged_as_given_for_every_outcome_and_only_none_keeps_the_estimate(outcome, cost, charged):
    assert StateCore._charge_micro(outcome, cost) == charged                                  # noqa: SLF001


def test_the_actual_charge_of_an_abandoned_ask_is_its_cost_or_the_estimate_when_unknown():
    backend = core()
    assert backend._actual_micro(60_000, "abandoned", 0) == 0                                  # noqa: SLF001
    assert backend._actual_micro(60_000, "abandoned", 7_000) == 7_000                          # noqa: SLF001
    assert backend._actual_micro(60_000, "abandoned", None) == 60_000                          # noqa: SLF001


# ------------------------------------------------------------------------------------------------ the boot report

def sums(**fields) -> SimpleNamespace:
    base = dict(paid=3, spend_micro=180_000, per_ip={"a": 2, "b": 1}, foreign_leases=0, ip_events=())
    return SimpleNamespace(**{**base, **fields})


def report_of(backend: StateCore, ledger_sums) -> RebuildReport:
    return backend._boot_report(day=DAY, now_wall=1.0, now_mono=2.0, expired=0, sums=ledger_sums,        # noqa: SLF001
                                synced=None, delta=None)


def test_the_boot_report_carries_the_per_address_spend_of_the_ledgers_sums():
    report = report_of(core(), sums(per_ip_spend={"a": 120_000, "b": 60_000}))
    assert report.per_ip_spend == {"a": 120_000, "b": 60_000} and report.per_ip == {"a": 2, "b": 1}


def test_sums_without_a_per_address_spend_give_an_empty_one():
    assert report_of(core(), sums()).per_ip_spend == {}


def test_a_report_built_without_a_per_address_spend_has_an_empty_one():
    report = RebuildReport(day=DAY, paid=0, spend_micro=0, per_ip={}, expired=0, foreign_leases=0, counters_synced=None,
                           delta=None, ip_events=(), now_wall=1.0, now_mono=2.0)
    assert report.per_ip_spend == {}


def test_the_report_copies_the_ledgers_mapping_instead_of_aliasing_it():
    ledger_sums = sums(per_ip_spend={"a": 1})
    report = report_of(core(), ledger_sums)
    ledger_sums.per_ip_spend["a"] = 99
    assert report.per_ip_spend == {"a": 1}


def test_a_boot_in_a_day_already_past_half_the_count_cap_warns_once_at_boot(caplog):
    backend = core()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        report_of(backend, sums(paid=MAX_COUNT // 2, spend_micro=0))
        backend._note_level(DAY, MAX_COUNT // 2 + 1, 0)                                         # noqa: SLF001
    half = [m for m in warnings_of(caplog) if m.startswith("state_day_half")]
    assert len(half) == 1 and "kind=count" in half[0]


# ------------------------------------------------------------------------------------------------ the warnings

def test_the_count_warning_comes_once_at_half_the_cap_and_not_before_or_again(caplog):
    backend = core()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for paid in range(1, MAX_COUNT // 2):
            backend._note_level(DAY, paid, 0)                                                   # noqa: SLF001
        assert warnings_of(caplog) == []
        for paid in range(MAX_COUNT // 2, MAX_COUNT + 1):
            backend._note_level(DAY, paid, 0)                                                   # noqa: SLF001
    (line,) = warnings_of(caplog)
    assert line.startswith("state_day_half") and f"day={DAY}" in line and "kind=count" in line
    assert f"used={MAX_COUNT // 2}" in line and f"cap={MAX_COUNT}" in line


def test_the_spend_warning_comes_once_at_half_the_cap(caplog):
    backend = core()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        backend._note_level(DAY, 1, MAX_SPEND_MICRO // 2 - 1)                                   # noqa: SLF001
        assert warnings_of(caplog) == []
        backend._note_level(DAY, 2, MAX_SPEND_MICRO // 2)                                       # noqa: SLF001
        backend._note_level(DAY, 3, MAX_SPEND_MICRO)                                            # noqa: SLF001
    (line,) = warnings_of(caplog)
    assert "kind=spend" in line and f"used_micro={MAX_SPEND_MICRO // 2}" in line and f"cap_micro={MAX_SPEND_MICRO}" in line


def test_count_and_spend_each_warn_once_when_they_cross_together(caplog):
    backend = core()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        backend._note_level(DAY, MAX_COUNT // 2, MAX_SPEND_MICRO // 2)                          # noqa: SLF001
        backend._note_level(DAY, MAX_COUNT // 2 + 1, MAX_SPEND_MICRO // 2 + 1)                  # noqa: SLF001
    assert sorted("count" if "kind=count" in m else "spend" for m in warnings_of(caplog)) == ["count", "spend"]


def test_a_new_day_warns_again(caplog):
    backend = core()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        backend._note_level(DAY, MAX_COUNT, 0)                                                  # noqa: SLF001
        backend._note_level(NEXT_DAY, MAX_COUNT, 0)                                             # noqa: SLF001
    lines = warnings_of(caplog)
    assert len(lines) == 2 and f"day={DAY}" in lines[0] and f"day={NEXT_DAY}" in lines[1]


@pytest.mark.parametrize("caps, silent", [(dict(max_queries_per_day=0), "kind=count"),
                                          (dict(max_spend_usd_per_day=0), "kind=spend")])
def test_a_cap_that_is_off_never_warns(caps, silent, caplog):
    backend = core(**caps)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        backend._note_level(DAY, 10**6, 10**12)                                                 # noqa: SLF001
    lines = warnings_of(caplog)
    assert len(lines) == 1 and silent not in lines[0]                                    # the other cap still warns


def test_a_pause_warns_once_per_day_and_per_reason(caplog):
    backend = core()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for _ in range(3):
            backend._note_pause(DAY, Denied.DAILY_COUNT)                                        # noqa: SLF001
        backend._note_pause(DAY, "daily_spend")                                                 # noqa: SLF001
        backend._note_pause(DAY, "daily_spend")                                                 # noqa: SLF001
        backend._note_pause(NEXT_DAY, Denied.DAILY_COUNT)                                       # noqa: SLF001
    lines = warnings_of(caplog)
    assert len(lines) == 3 and all(m.startswith("state_day_paused") for m in lines)
    assert sorted(m.split("reason=")[1].split()[0] for m in lines) == ["daily_count", "daily_count", "daily_spend"]


def test_a_pause_is_a_separate_warning_from_the_half_way_one(caplog):
    backend = core()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        backend._note_level(DAY, MAX_COUNT, 0)                                                  # noqa: SLF001
        backend._note_pause(DAY, Denied.DAILY_COUNT)                                            # noqa: SLF001
    assert [m.split()[0] for m in warnings_of(caplog)] == ["state_day_half", "state_day_paused"]


def test_the_notes_keep_a_few_days_at_most(caplog):
    backend = core()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for n in range(30):
            backend._note_level(f"2026-11-{n + 1:02d}", MAX_COUNT, 0)                           # noqa: SLF001
    assert len(warnings_of(caplog)) == 30 and len(backend._noted) <= 3                         # noqa: SLF001


def test_sixteen_threads_noting_the_same_crossing_log_it_exactly_once(caplog):
    backend = core()
    barrier = threading.Barrier(16)

    def note() -> None:
        barrier.wait(timeout=30)
        backend._note_level(DAY, MAX_COUNT, MAX_SPEND_MICRO)                                    # noqa: SLF001
        backend._note_pause(DAY, Denied.DAILY_SPEND)                                            # noqa: SLF001

    with caplog.at_level(logging.INFO, logger=LOGGER):
        threads = [threading.Thread(target=note) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert sorted(m.split()[0] for m in warnings_of(caplog)) == ["state_day_half", "state_day_half", "state_day_paused"]

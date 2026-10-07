"""The named limiters and the event-loop lag monitor of the async answer path (M5a I2)."""

import asyncio
import logging
import time
from types import SimpleNamespace

import anyio
import pytest

from semigraph.serve.limiters import Limiters, LoopLagMonitor, make_limiters


def settings(**over):
    base = dict(embed_slots=1, db_thread_limit=32, loop_lag_warn_ms=100)
    return SimpleNamespace(**{**base, **over})


def test_make_limiters_sizes_each_pool_from_the_settings():
    async def build():
        return make_limiters(settings(embed_slots=2, db_thread_limit=7))

    lim = asyncio.run(build())

    assert isinstance(lim, Limiters)
    assert lim.embed.total_tokens == 2 and lim.db.total_tokens == 7
    assert lim.health.total_tokens == 1 and lim.state.total_tokens == 4
    assert lim.admin.total_tokens == 1                      # the admin routes' own one, apart from the public state pool
    assert len({id(pool) for pool in lim}) == len(lim)      # five pools, none of them shared


def test_a_limiters_tuple_built_without_the_admin_pool_still_works_for_everything_but_the_admin_routes():
    """The pools are named fields: a test double that predates ``admin`` builds the other four."""
    async def build():
        return Limiters(embed=anyio.CapacityLimiter(1), db=anyio.CapacityLimiter(1), health=anyio.CapacityLimiter(1),
                        state=anyio.CapacityLimiter(1))

    assert asyncio.run(build()).admin is None


@pytest.mark.parametrize("field", ["embed_slots", "db_thread_limit"])
def test_a_pool_of_zero_threads_is_refused(field):
    async def build():
        return make_limiters(settings(**{field: 0}))

    with pytest.raises(ValueError, match=field):
        asyncio.run(build())


def test_the_monitor_warns_when_the_loop_is_blocked(caplog):
    monitor = LoopLagMonitor(warn_ms=100, interval_s=0.02)

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(monitor.run)
            await anyio.sleep(0.1)
            time.sleep(0.3)               # a blocking call ON the loop: exactly what the monitor exists to catch
            await anyio.sleep(0.1)
            tg.cancel_scope.cancel()

    with caplog.at_level(logging.WARNING, logger="semigraph.serve.loop"):
        asyncio.run(scenario())

    assert monitor.warnings >= 1 and monitor.max_lag_ms >= 150
    assert any("loop_lag_ms" in r.getMessage() for r in caplog.records)


def test_the_monitor_is_silent_when_the_loop_is_free(caplog):
    monitor = LoopLagMonitor(warn_ms=200, interval_s=0.02)

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(monitor.run)
            for _ in range(10):
                await anyio.sleep(0.02)   # cooperative work only
            tg.cancel_scope.cancel()

    with caplog.at_level(logging.WARNING, logger="semigraph.serve.loop"):
        asyncio.run(scenario())

    assert monitor.warnings == 0 and not caplog.records


def test_the_monitor_rejects_nonsense_settings():
    with pytest.raises(ValueError):
        LoopLagMonitor(warn_ms=0)
    with pytest.raises(ValueError):
        LoopLagMonitor(warn_ms=100, interval_s=0)

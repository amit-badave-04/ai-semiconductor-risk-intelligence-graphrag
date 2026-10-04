"""The four settings of the async answer path (M5a I2) and their lower bound of 1.

A ``send_timeout_s`` of 0 or less would drop EVERY paid stream after retrieval (``anyio.move_on_after(0)`` is already
cancelled), and a zero ``embed_slots`` or ``db_thread_limit`` is a limiter nothing can ever take: each is a refusal at
start-up, where a bad ``.env`` shows at once, not a service that answers nobody. Only ``semigraph.config`` and pydantic
are imported (this file runs in the CI job that has no pandas, sentence-transformers or neo4j).
"""

import pytest
from pydantic import ValidationError

from semigraph.config import Settings

# field -> (environment variable, default)
ASYNC_SETTINGS = {"embed_slots": ("EMBED_SLOTS", 1), "db_thread_limit": ("DB_THREAD_LIMIT", 32),
                  "send_timeout_s": ("SEND_TIMEOUT_S", 30), "loop_lag_warn_ms": ("LOOP_LAG_WARN_MS", 100)}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """A variable left in the developer's shell must neither break the defaults test nor leak into another one."""
    for env, _ in ASYNC_SETTINGS.values():
        monkeypatch.delenv(env, raising=False)


def settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


def test_the_defaults_are_valid_and_unchanged():
    s = settings()
    assert {name: getattr(s, name) for name in ASYNC_SETTINGS} == {name: d for name, (_, d) in ASYNC_SETTINGS.items()}


@pytest.mark.parametrize("name", ASYNC_SETTINGS)
@pytest.mark.parametrize("bad", [0, -1, -30])
def test_a_value_below_one_is_refused_and_names_the_field(name, bad):
    with pytest.raises(ValidationError, match=name):
        settings(**{name: bad})


@pytest.mark.parametrize("name", ASYNC_SETTINGS)
def test_one_is_the_smallest_accepted_value(name):
    assert getattr(settings(**{name: 1}), name) == 1


@pytest.mark.parametrize("name", ASYNC_SETTINGS)
def test_an_environment_variable_overrides_the_default(monkeypatch, name):
    monkeypatch.setenv(ASYNC_SETTINGS[name][0], "45")
    assert getattr(settings(), name) == 45


@pytest.mark.parametrize("name", ASYNC_SETTINGS)
@pytest.mark.parametrize("bad", ["0", "-5"])
def test_an_environment_variable_below_one_is_refused(monkeypatch, name, bad):
    monkeypatch.setenv(ASYNC_SETTINGS[name][0], bad)
    with pytest.raises(ValidationError, match=name):
        settings()


def test_the_bound_is_only_on_these_four_settings():
    """``0`` still means "off" or "unlimited" where it always did."""
    s = settings(max_queries_per_day=0, onnx_threads=0, answer_cache_ttl_hours=0)
    assert (s.max_queries_per_day, s.onnx_threads, s.answer_cache_ttl_hours) == (0, 0, 0)

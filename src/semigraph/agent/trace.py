"""The tracing seam of the agent: a duck-typed :class:`Tracer`, a no-op default and a wrapper that makes any tracer safe.

The contract (docs/v2/M3_AGENT_PLAN.md section 6): ``span(name, **attrs)`` is a context manager whose handle has
``set(**attrs)``; ``event(name, **attrs)``; ``generation(*, name, model, usage, cost_usd, input_chars, output_chars)``;
``flush()``. NONE of them may ever raise into the answer path. A tracer is observability: a broken collector must cost a
trace, never an answer, so the agent wraps whatever it is given in :class:`SafeTracer` (which logs and swallows a tracer's
own failures, and never touches an exception raised by the traced code).

What a tracer is given is the caller's choice; the agent itself passes lengths, counts and ids, never the question, the answer
or a hash of either.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("semigraph.agent.trace")


@runtime_checkable
class SpanHandle(Protocol):
    def set(self, **attrs: Any) -> None: ...


@runtime_checkable
class Tracer(Protocol):
    def span(self, name: str, **attrs: Any) -> AbstractContextManager[SpanHandle]: ...

    def event(self, name: str, **attrs: Any) -> None: ...

    def generation(self, *, name: str, model: str, usage: dict | None, cost_usd: float | None,
                   input_chars: int, output_chars: int) -> None: ...

    def flush(self) -> None: ...


class _NullSpan:
    def set(self, **attrs: Any) -> None:
        return None


NULL_SPAN = _NullSpan()


class NullTracer:
    """The default: records nothing."""

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[SpanHandle]:
        yield NULL_SPAN

    def event(self, name: str, **attrs: Any) -> None:
        return None

    def generation(self, *, name: str, model: str, usage: dict | None, cost_usd: float | None,
                   input_chars: int, output_chars: int) -> None:
        return None

    def flush(self) -> None:
        return None


_reported: set[tuple[str, str]] = set()      # (method, exception type) pairs already logged at WARNING


def _call(what: str, fn: Callable[[], Any]) -> tuple[bool, Any]:
    """``(True, fn())``, or ``(False, None)`` when it raised. ``fn`` is a closure so that even LOOKING UP the tracer's method
    (an object with no such method) happens inside the guard. The first failure of each kind is a WARNING (a broken collector must
    be noticed), the repeats are DEBUG (it would otherwise log on every request)."""
    try:
        return True, fn()
    except Exception as e:  # noqa: BLE001 - a tracer must never break an answer
        kind = (what, type(e).__name__)
        logger.log(logging.DEBUG if kind in _reported else logging.WARNING, "tracer %s failed: %s", what, type(e).__name__)
        _reported.add(kind)
        return False, None


class _SafeHandle:
    def __init__(self, inner: Any):
        self._inner = inner

    def set(self, **attrs: Any) -> None:
        if self._inner is not None:
            _call("span.set", lambda: self._inner.set(**attrs))


class SafeTracer:
    """Wraps a tracer so that none of its methods can raise."""

    def __init__(self, inner: Tracer):
        self._inner = inner

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[SpanHandle]:
        made, manager = _call("span", lambda: self._inner.span(name, **attrs))
        entered, handle = _call("span.__enter__", lambda: manager.__enter__()) if made else (False, None)
        try:
            yield _SafeHandle(handle)
        except BaseException as exc:
            if entered:
                _call("span.__exit__", lambda: manager.__exit__(type(exc), exc, exc.__traceback__))
            raise
        else:
            if entered:
                _call("span.__exit__", lambda: manager.__exit__(None, None, None))

    def event(self, name: str, **attrs: Any) -> None:
        _call("event", lambda: self._inner.event(name, **attrs))

    def generation(self, *, name: str, model: str, usage: dict | None, cost_usd: float | None,
                   input_chars: int, output_chars: int) -> None:
        _call("generation", lambda: self._inner.generation(name=name, model=model, usage=usage, cost_usd=cost_usd,
                                                           input_chars=input_chars, output_chars=output_chars))

    def flush(self) -> None:
        _call("flush", lambda: self._inner.flush())


def as_safe(tracer: Tracer | None) -> Tracer:
    """``tracer`` made safe: None is the no-op tracer, an already safe (or no-op) tracer is returned as it is."""
    if tracer is None:
        return NullTracer()
    return tracer if isinstance(tracer, (SafeTracer, NullTracer)) else SafeTracer(tracer)

"""The tracing seam of the agent (docs/v2/M3_AGENT_PLAN.md section 6): a Tracer Protocol, a NullTracer default and a wrapper that
makes ANY tracer safe. The contract: none of a tracer's methods may ever raise into the answer path."""

import pytest

from semigraph.agent.trace import NULL_SPAN, NullTracer, SafeTracer, Tracer, as_safe


class RecordingTracer:
    def __init__(self):
        self.log = []

    def span(self, name, **attrs):
        tracer = self

        class _Span:
            def __enter__(self_inner):
                tracer.log.append(("enter", name, attrs))
                return self_inner

            def __exit__(self_inner, *exc):
                tracer.log.append(("exit", name, exc[0].__name__ if exc[0] else None))
                return False

            def set(self_inner, **more):
                tracer.log.append(("set", name, more))

        return _Span()

    def event(self, name, **attrs):
        self.log.append(("event", name, attrs))

    def generation(self, *, name, model, usage, cost_usd, input_chars, output_chars):
        self.log.append(("generation", name, model, usage, cost_usd, input_chars, output_chars))

    def flush(self):
        self.log.append(("flush",))


class ExplodingTracer:
    """Every method raises, including entering and leaving a span."""

    def span(self, name, **attrs):
        raise RuntimeError("span boom")

    def event(self, name, **attrs):
        raise RuntimeError("event boom")

    def generation(self, **kw):
        raise RuntimeError("generation boom")

    def flush(self):
        raise RuntimeError("flush boom")


class ExplodingSpanTracer(RecordingTracer):
    """The span object exists but raises on enter / set / exit."""

    def span(self, name, **attrs):
        class _Bad:
            def __enter__(self):
                raise RuntimeError("enter boom")

            def __exit__(self, *exc):
                raise RuntimeError("exit boom")

        return _Bad()


class ExplodingHandleTracer(RecordingTracer):
    def span(self, name, **attrs):
        class _Span:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                raise RuntimeError("exit boom")

            def set(self, **more):
                raise RuntimeError("set boom")

        return _Span()


def test_the_null_tracer_does_nothing_and_satisfies_the_protocol():
    tracer = NullTracer()
    assert isinstance(tracer, Tracer)
    with tracer.span("agent", a=1) as span:
        span.set(b=2)
        assert span is NULL_SPAN
    tracer.event("x", a=1)
    tracer.generation(name="planner", model="m", usage=None, cost_usd=None, input_chars=1, output_chars=1)
    tracer.flush()


def test_a_safe_tracer_forwards_every_call_to_a_healthy_tracer():
    inner = RecordingTracer()
    tracer = SafeTracer(inner)
    with tracer.span("plan", n=1) as span:
        span.set(ok=True)
    tracer.event("fallback", reason="x")
    tracer.generation(name="planner", model="m", usage={"prompt_tokens": 1}, cost_usd=0.1, input_chars=5, output_chars=2)
    tracer.flush()
    assert [entry[0] for entry in inner.log] == ["enter", "set", "exit", "event", "generation", "flush"]
    assert inner.log[0] == ("enter", "plan", {"n": 1}) and inner.log[2] == ("exit", "plan", None)


@pytest.mark.parametrize("bad", [ExplodingTracer, ExplodingSpanTracer, ExplodingHandleTracer])
def test_a_safe_tracer_survives_a_tracer_that_raises_from_every_method(bad):
    tracer = SafeTracer(bad())
    with tracer.span("agent", q=1) as span:
        span.set(x=1)
        ran = True
    tracer.event("e")
    tracer.generation(name="p", model="m", usage=None, cost_usd=None, input_chars=0, output_chars=0)
    tracer.flush()
    assert ran


def test_a_safe_span_never_swallows_or_alters_the_error_of_the_traced_code():
    inner = RecordingTracer()
    tracer = SafeTracer(inner)
    with pytest.raises(ValueError, match="real bug"), tracer.span("tools"):
        raise ValueError("real bug")
    assert ("exit", "tools", "ValueError") in inner.log
    with pytest.raises(ValueError, match="real bug"), SafeTracer(ExplodingHandleTracer()).span("tools"):
        raise ValueError("real bug")


def test_a_span_that_yields_nothing_is_still_entered_and_exited():
    """A ``@contextmanager`` span (or ``nullcontext``) yields None: the span was entered and MUST be exited."""
    from contextlib import contextmanager, nullcontext

    log = []

    class Yielding:
        @contextmanager
        def span(self, name, **attrs):
            log.append(("enter", name))
            try:
                yield
            finally:
                log.append(("exit", name))

        def event(self, *a, **k):
            pass

        def generation(self, **k):
            pass

        def flush(self):
            pass

    tracer = SafeTracer(Yielding())
    with tracer.span("tools") as span:
        span.set(ok=True)                       # a handle of None: set must not raise
    assert log == [("enter", "tools"), ("exit", "tools")]

    class Null(Yielding):
        def span(self, name, **attrs):
            return nullcontext()

    with SafeTracer(Null()).span("x") as span:
        span.set(a=1)


def test_a_tracer_whose_span_is_not_a_context_manager_or_that_lacks_methods_is_survived():
    class NotAManager(RecordingTracer):
        def span(self, name, **attrs):
            return object()

    with SafeTracer(NotAManager()).span("x") as span:
        span.set(a=1)
    bare = SafeTracer(object())                 # no method at all
    with bare.span("x") as span:
        span.set(a=1)
    bare.event("e")
    bare.generation(name="p", model="m", usage=None, cost_usd=None, input_chars=0, output_chars=0)
    bare.flush()


def test_a_broken_tracer_is_reported_once_at_warning_and_then_quietly(caplog, monkeypatch):
    from semigraph.agent import trace

    monkeypatch.setattr(trace, "_reported", set())
    tracer = SafeTracer(ExplodingTracer())
    with caplog.at_level("DEBUG", logger="semigraph.agent.trace"):
        for _ in range(3):
            tracer.event("e")
    levels = [record.levelname for record in caplog.records]
    assert levels == ["WARNING", "DEBUG", "DEBUG"]


def test_as_safe_wraps_once_and_defaults_to_the_null_tracer():
    assert isinstance(as_safe(None), NullTracer)
    wrapped = as_safe(RecordingTracer())
    assert isinstance(wrapped, SafeTracer) and as_safe(wrapped) is wrapped
    null = NullTracer()
    assert as_safe(null) is null

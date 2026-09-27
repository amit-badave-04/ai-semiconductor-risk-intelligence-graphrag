"""serve/tracing.py: the fail-open, privacy-by-default Langfuse tracer (docs/v2/M3_AGENT_PLAN.md section 6).

The real ``langfuse`` package is NOT installed in the venv (it is an optional extra), so every test injects a fake module into
``sys.modules``. The fake mirrors the v4 calls the tracer uses (``Langfuse(...)``, ``start_observation(name=, as_type=, ...)`` on
the client and on an observation, ``update(...)``, ``end()``, ``flush()``, ``shutdown()``). What a fake observation RECEIVES is
what would leave the process, so the privacy tests scan it for canary strings."""

import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from semigraph.serve import tracing

QUESTION = "Which HBM suppliers does Nvidia depend on, and what did the CFO say in the Zurich memo?"
ANSWER = "Nvidia depends on SK hynix and Micron for HBM; the Zurich memo is not in the filings."
IP = "203.0.113.77"
BEARER = "Bearer abc123-canary-bearer"
PUBLIC = "pk-lf-public-canary"
SECRET = "sk-lf-secret-canary"
HOST = "https://langfuse.example.test"
CANARIES = (QUESTION, ANSWER, "Zurich", "CFO", IP, "abc123-canary", SECRET, PUBLIC)


class FakeObservation:
    """One recorded observation: what was passed at creation, every update and whether it ended."""

    def __init__(self, client, parent, kwargs):
        self.client, self.parent, self.kwargs = client, parent, kwargs
        self.updates: list[dict] = []
        self.ended = False
        self.children: list["FakeObservation"] = []

    def start_observation(self, **kwargs):
        self.client.maybe_fail("start_observation")
        child = FakeObservation(self.client, self, kwargs)
        self.children.append(child)
        self.client.everything.append(child)
        return child

    def update(self, **kwargs):
        self.client.maybe_fail("update")
        self.updates.append(kwargs)

    def end(self, **kwargs):
        self.client.maybe_fail("end")
        self.ended = True


class FakeLangfuse:
    """The Langfuse client: keeps every observation it created, in order."""

    instances: list["FakeLangfuse"] = []
    fail_on: set[str] = set()
    flush_gate: threading.Event | None = None

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.roots: list[FakeObservation] = []
        self.everything: list[FakeObservation] = []
        self.flush_calls = 0
        self.shutdown_calls = 0
        FakeLangfuse.instances.append(self)

    def maybe_fail(self, what):
        if what in FakeLangfuse.fail_on:
            raise RuntimeError(f"langfuse {what} exploded ({SECRET})")

    def start_observation(self, **kwargs):
        self.maybe_fail("start_observation")
        root = FakeObservation(self, None, kwargs)
        self.roots.append(root)
        self.everything.append(root)
        return root

    def flush(self):
        self.maybe_fail("flush")
        if FakeLangfuse.flush_gate is not None:
            FakeLangfuse.flush_gate.wait(timeout=5)
        self.flush_calls += 1

    def shutdown(self):
        self.maybe_fail("shutdown")
        self.shutdown_calls += 1


@pytest.fixture
def langfuse(monkeypatch):
    """Inject the fake package; returns the module so a test can read ``Langfuse.instances``."""
    FakeLangfuse.instances, FakeLangfuse.fail_on, FakeLangfuse.flush_gate = [], set(), None
    module = types.ModuleType("langfuse")
    module.Langfuse = FakeLangfuse
    span_filter = types.ModuleType("langfuse.span_filter")        # the documented home of the export predicates (v4)
    span_filter.is_langfuse_span = lambda span: True
    module.span_filter = span_filter
    monkeypatch.setitem(sys.modules, "langfuse", module)
    monkeypatch.setitem(sys.modules, "langfuse.span_filter", span_filter)
    return module


def settings(**over):
    base = dict(langfuse_public_key=PUBLIC, langfuse_secret_key=SECRET, langfuse_host=HOST, langfuse_sample_rate=1.0)
    return SimpleNamespace(**{**base, **over})


def tracer_and_client(**over):
    tracer = tracing.get_tracer(settings(**over.pop("settings", {})), **over)
    return tracer, (FakeLangfuse.instances[-1] if FakeLangfuse.instances else None)


def sent(client) -> str:
    """Everything the fake observations received (creation kwargs + updates), as one JSON string. The constructor is excluded:
    it legitimately receives the keys."""
    payload = [{"created": o.kwargs, "updates": o.updates} for o in client.everything]
    return json.dumps(payload, default=repr)


def metadata_of(obs: FakeObservation) -> dict:
    merged = dict(obs.kwargs.get("metadata") or {})
    for update in obs.updates:
        merged.update(update.get("metadata") or {})
    return merged


# ---------------------------------------------------------------- off by default, no import

@pytest.mark.parametrize("missing", ["langfuse_public_key", "langfuse_secret_key", "langfuse_host"])
def test_tracing_is_a_no_op_unless_the_public_key_the_secret_key_and_the_host_are_all_set(monkeypatch, caplog, missing):
    monkeypatch.setitem(sys.modules, "langfuse", None)              # importing it would raise: it must never be attempted
    with caplog.at_level(logging.DEBUG):
        tracer = tracing.get_tracer(settings(**{missing: ""}))
    assert tracer.enabled is False and caplog.records == []


def test_a_settings_object_without_any_langfuse_field_is_a_no_op(monkeypatch):
    monkeypatch.setitem(sys.modules, "langfuse", None)
    assert tracing.get_tracer(SimpleNamespace()).enabled is False   # test doubles such as FakeSettings carry none of the fields


def test_a_langfuse_without_the_export_filter_is_refused_rather_than_run_unfiltered(monkeypatch, caplog):
    """The default filter also exports spans from known LLM instrumentation scopes (a litellm callback would ship prompts)."""
    module = types.ModuleType("langfuse")
    module.Langfuse = FakeLangfuse
    monkeypatch.setitem(sys.modules, "langfuse", module)
    monkeypatch.setitem(sys.modules, "langfuse.span_filter", None)
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.tracing"):
        assert tracing.get_tracer(settings()).enabled is False
    assert any("span_filter" in r.getMessage() for r in caplog.records)


def test_the_export_filter_falls_back_to_the_top_level_attribute_before_tracing_is_refused(monkeypatch):
    module = types.ModuleType("langfuse")
    module.Langfuse = FakeLangfuse
    module.is_langfuse_span = lambda span: True
    FakeLangfuse.instances, FakeLangfuse.fail_on = [], set()
    monkeypatch.setitem(sys.modules, "langfuse", module)
    monkeypatch.setitem(sys.modules, "langfuse.span_filter", None)             # the submodule is absent on this version
    assert tracing.get_tracer(settings()).enabled is True
    assert FakeLangfuse.instances[-1].init_kwargs["should_export_span"] is module.is_langfuse_span


def test_a_zero_sample_rate_never_builds_a_client(langfuse):
    assert tracing.get_tracer(settings(langfuse_sample_rate=0.0)).enabled is False and FakeLangfuse.instances == []


def test_the_null_tracer_speaks_the_whole_contract_and_never_raises():
    tracer = tracing.get_tracer(SimpleNamespace())
    with tracer.span("agent", strategy="agent", anything=object()) as span:
        span.set(tool="lookup_company", n=1)
        tracer.event("step", tool="x")
        tracer.generation(name="planner", model="m", usage={"prompt_tokens": 1}, cost_usd=0.1, input_chars=1, output_chars=2)
    tracer.flush()
    per_request = tracer.for_request(QUESTION, strategy="agent")
    with per_request.span("plan"):
        pass
    per_request.close()
    tracer.shutdown()


# ---------------------------------------------------------------- the client is configured as agreed

def test_the_client_gets_the_keys_the_host_our_own_sampling_and_an_export_filter(langfuse):
    tracing.get_tracer(settings())
    kwargs = FakeLangfuse.instances[-1].init_kwargs
    assert kwargs["public_key"] == PUBLIC and kwargs["secret_key"] == SECRET
    assert kwargs["base_url"] == HOST and "host" not in kwargs            # `host` is deprecated in v4
    assert kwargs["sample_rate"] == 1.0                                   # we sample ourselves: a stray env var must not sample twice
    assert kwargs["timeout"] <= 5 and callable(kwargs["mask"])
    assert kwargs["should_export_span"] is langfuse.span_filter.is_langfuse_span   # only OUR observations leave, never a litellm / OTel span


def test_the_masking_hook_lets_labels_and_numbers_through_and_masks_free_text(langfuse):
    tracing.get_tracer(settings())
    mask = FakeLangfuse.instances[-1].init_kwargs["mask"]
    assert mask(data={"tool": "lookup_company", "n": 2, "note": ANSWER}) == {"tool": "lookup_company", "n": 2, "note_chars": len(ANSWER)}
    assert mask(data=ANSWER) == "[masked]" and mask(data="openai/gpt-6-luna") == "openai/gpt-6-luna" and mask(data=None) is None


# ---------------------------------------------------------------- sampling: one deterministic decision per request

def test_the_sampling_decision_is_made_once_per_request_from_the_injected_random_source(langfuse):
    draws = iter([0.05, 0.5, 0.099, 0.1, 0.0])
    tracer, client = tracer_and_client(rng=lambda: next(draws), settings={"langfuse_sample_rate": 0.1})
    decisions = []
    for _ in range(5):
        request = tracer.for_request("q", strategy="agent")
        with request.span("agent"):
            request.event("step", tool="t")
        request.close()
        decisions.append(len(client.roots))
    assert decisions == [1, 1, 2, 2, 3]                                   # sampled: draws 1, 3 and 5 (strictly below the rate)


def test_an_unsampled_request_never_touches_the_client(langfuse):
    tracer, client = tracer_and_client(rng=lambda: 0.99, settings={"langfuse_sample_rate": 0.1})
    request = tracer.for_request(QUESTION, strategy="agent")
    with request.span("agent", question=QUESTION) as span:
        span.set(n=1)
        request.event("step")
        request.generation(name="planner", model="m", usage={"prompt_tokens": 3}, cost_usd=0.1, input_chars=1, output_chars=1)
    request.flush()
    request.close()
    assert client.everything == []


def test_a_rate_of_one_samples_every_request_without_drawing(langfuse):
    def no_draw():
        raise AssertionError("a rate of 1.0 must not consume the random source")
    tracer, client = tracer_and_client(rng=no_draw)
    tracer.for_request("q").close()
    assert len(client.roots) == 1


# ---------------------------------------------------------------- the trace a sampled request produces

def test_a_sampled_request_is_one_root_with_nested_spans_events_and_a_priced_generation(langfuse):
    tracer, client = tracer_and_client()
    request = tracer.for_request(QUESTION, strategy="agent")
    with request.span("agent", strategy="agent") as agent:
        with request.span("tool", tool="financial_metrics") as tool:
            request.event("step", n=1, tool="financial_metrics", ok=True)
            tool.set(ok=True, elapsed_s=0.42)
        request.generation(name="planner", model="openai/gpt-6-luna", input_chars=812, output_chars=44,
                           usage={"prompt_tokens": 1000, "completion_tokens": 50}, cost_usd=0.00041)
        agent.set(fallback_reason=None, model_calls=1)
    request.close()

    (root,) = client.roots
    assert root.kwargs["name"] == "ask" and root.ended
    assert metadata_of(root)["strategy"] == "agent" and metadata_of(root)["question_chars"] == len(QUESTION)
    assert isinstance(metadata_of(root)["latency_s"], (int, float))
    (agent_obs,) = root.children
    assert agent_obs.kwargs["name"] == "agent" and agent_obs.ended and metadata_of(agent_obs)["model_calls"] == 1
    assert "fallback_reason" not in metadata_of(agent_obs)                      # None is not recorded
    tool_obs, generation = agent_obs.children
    assert tool_obs.kwargs["name"] == "tool" and metadata_of(tool_obs) == {"tool": "financial_metrics", "ok": True, "elapsed_s": 0.42}
    (event,) = tool_obs.children
    assert event.kwargs["as_type"] == "event" and metadata_of(event) == {"n": 1, "tool": "financial_metrics", "ok": True} and event.ended
    assert generation.kwargs["as_type"] == "generation" and generation.kwargs["model"] == "openai/gpt-6-luna"
    assert generation.kwargs["usage_details"] == {"input": 1000, "output": 50, "total": 1050}   # the Langfuse docs' key convention
    assert generation.kwargs["cost_details"] == {"total": 0.00041}
    assert metadata_of(generation) == {"input_chars": 812, "output_chars": 44} and generation.ended


def test_close_ends_spans_a_caller_left_open_and_is_idempotent(langfuse):
    tracer, client = tracer_and_client()
    request = tracer.for_request("q")
    request.span("agent").__enter__()                                        # e.g. the client vanished before __exit__ ran
    request.close()
    request.close()
    (root,) = client.roots
    assert root.ended and all(o.ended for o in client.everything)


def test_the_question_hash_is_salted_stable_and_not_the_question(langfuse):
    first, client = tracer_and_client(salt="salt-A")
    first.for_request(QUESTION).close()
    first.for_request(QUESTION).close()
    second, client_b = tracer_and_client(salt="salt-B")
    second.for_request(QUESTION).close()
    h1, h2 = (metadata_of(r)["question_hash"] for r in client.roots)
    (h3,) = (metadata_of(r)["question_hash"] for r in client_b.roots)
    assert h1 == h2 and h1 != h3 and len(h1) == 16 and int(h1, 16) >= 0
    assert QUESTION not in sent(client) and "Zurich" not in sent(client)


def secret_derived_hashes(text: str) -> set[str]:
    """Every way a hash could be derived from the Langfuse secret: the recipient of the traces holds that secret (the OTLP
    exporter authenticates with it), so none of these may ever be the question hash."""
    out = set()
    for key in (SECRET.encode(), b"semigraph-trace-v1|" + SECRET.encode(), (PUBLIC + ":" + SECRET).encode()):
        out.add(hmac.new(key, text.encode(), hashlib.sha256).hexdigest()[:16])
    out.add(hashlib.sha256((SECRET + text).encode()).hexdigest()[:16])
    return out


def test_without_a_salt_the_hash_is_random_per_process_and_not_derived_from_the_secret_the_recipient_holds(langfuse):
    """The default salt must not be reproducible by whoever receives the traces (they hold the secret key): a random per-process
    key groups repeats within one run and lets nobody confirm a guessed question."""
    first, client_a = tracer_and_client()
    first.for_request(QUESTION).close()
    first.for_request(QUESTION).close()
    second, client_b = tracer_and_client()
    second.for_request(QUESTION).close()
    h1, h2 = (metadata_of(r)["question_hash"] for r in client_a.roots)
    (h3,) = (metadata_of(r)["question_hash"] for r in client_b.roots)
    assert h1 == h2                                                          # stable within one tracer
    assert h1 != h3                                                          # another process (tracer) draws another salt
    assert h1 not in secret_derived_hashes(QUESTION) and h3 not in secret_derived_hashes(QUESTION)
    assert SECRET not in sent(client_a)


def test_a_configured_hash_salt_groups_repeats_across_restarts_and_an_explicit_salt_wins(langfuse):
    configured = {"langfuse_hash_salt": "owner-chosen-salt"}
    a, client_a = tracer_and_client(settings=dict(configured))
    a.for_request(QUESTION).close()
    b, client_b = tracer_and_client(settings=dict(configured))                # a restart
    b.for_request(QUESTION).close()
    c, client_c = tracer_and_client(settings=dict(configured), salt="explicit")
    c.for_request(QUESTION).close()
    ha, hb, hc = (metadata_of(cl.roots[0])["question_hash"] for cl in (client_a, client_b, client_c))
    assert ha == hb and hc != ha
    assert ha not in secret_derived_hashes(QUESTION) and "owner-chosen-salt" not in sent(client_a)


def test_using_the_factory_directly_still_produces_a_flat_trace_per_call(langfuse):
    """A caller that never asked for a per-request tracer (a script, an eval) still gets traces, one per top-level call."""
    tracer, client = tracer_and_client()
    with tracer.span("agent", strategy="agent"):
        pass
    tracer.event("step", tool="t")
    tracer.generation(name="planner", model="m", usage=None, cost_usd=None, input_chars=1, output_chars=1)
    assert len(client.roots) == 3 and all(r.ended for r in client.everything)


# ---------------------------------------------------------------- privacy by default

def hostile_calls(tracer):
    """Every way a caller could hand the tracer text that must not leave the process."""
    request = tracer.for_request(QUESTION, strategy="agent")
    with request.span("agent", question=QUESTION, answer=ANSWER, ip=IP, client_ip=IP, headers={"authorization": BEARER, "x": "y"},
                      token=SECRET, api_key=SECRET, authorization=BEARER, prompt=QUESTION, text=ANSWER,
                      args={"query": QUESTION, "company": "Nvidia"}, note=ANSWER, tool="lookup_company") as span:
        span.set(question=QUESTION, output=ANSWER, summary=ANSWER, detail=ANSWER, cookie=BEARER, ok=True)
        request.event("step", n=1, tool="search_filings", summary=ANSWER, args={"query": QUESTION}, ip=IP)
        request.generation(name="planner", model="openai/gpt-6-luna", usage={"prompt_tokens": 10, "completion_tokens": 2,
                                                                              "note": ANSWER},
                           cost_usd=0.001, input_chars=len(QUESTION), output_chars=len(ANSWER),
                           input=QUESTION, output=ANSWER, question=QUESTION)
        request.generation(name=ANSWER, model=QUESTION, usage=None, cost_usd=None, input_chars=1, output_chars=1)
        with pytest.raises(ValueError):
            with request.span("tool", tool="t", fallback_reason=f"planner failed on {QUESTION}"):
                raise ValueError(f"boom: {QUESTION} {ANSWER} {IP}")
    request.close()


def test_nothing_the_fake_client_receives_carries_the_question_the_answer_an_ip_a_header_a_token_or_a_key(langfuse):
    tracer, client = tracer_and_client()
    hostile_calls(tracer)
    payload = sent(client)
    for canary in CANARIES:
        assert canary not in payload, canary
    for obs in client.everything:
        assert "input" not in obs.kwargs and "output" not in obs.kwargs and all("input" not in u and "output" not in u for u in obs.updates)


def test_the_useful_facts_still_arrive_next_to_the_scrubbed_ones(langfuse):
    tracer, client = tracer_and_client()
    hostile_calls(tracer)
    root = client.roots[0]
    agent = root.children[0]
    meta = metadata_of(agent)
    assert meta["tool"] == "lookup_company" and meta["ok"] is True
    assert meta["question_chars"] == len(QUESTION) and meta["answer_chars"] == len(ANSWER)
    assert meta["question_hash"] == metadata_of(root)["question_hash"]      # the same salted hash, wherever the text was offered
    step = next(o for o in client.everything if o.kwargs["name"] == "step")
    assert metadata_of(step)["tool"] == "search_filings" and metadata_of(step)["summary_chars"] == len(ANSWER)
    generation = next(o for o in client.everything if o.kwargs.get("model") == "openai/gpt-6-luna")
    assert generation.kwargs["usage_details"] == {"input": 10, "output": 2, "total": 12}


@pytest.mark.parametrize("attrs,expected", [
    ({"n": 3, "elapsed_s": 1.5, "ok": False, "cost_usd": 0.01}, {"n": 3, "elapsed_s": 1.5, "ok": False, "cost_usd": 0.01}),
    ({"tool": "compute_change", "model": "openai/gpt-6-luna", "strategy": "agent"},
     {"tool": "compute_change", "model": "openai/gpt-6-luna", "strategy": "agent"}),
    ({"fallback_reason": "planner_error: BadRequestError"}, {"fallback_reason": "planner_error"}),
    ({"fallback_reason": "time budget exhausted"}, {"fallback_reason": "time"}),
    ({"tool": "two words"}, {"tool_chars": 9}),
    ({"summary": "Fetched revenue for FY2024-2026"}, {"summary_chars": 31}),
    ({"prompt_tokens": 12, "token": "sk-x"}, {"prompt_tokens": 12}),
    ({"headers": {"a": "b"}, "ip": "1.2.3.4", "authorization": "Bearer x", "secret": "s", "password": "p"}, {}),
    ({"maybe": None, "nan": float("nan"), "inf": float("inf"), "obj": object()}, {"obj_type": "object"}),
    ({"1.2.3.4": 5, "has space": 1, "ok_key": 1}, {"ok_key": 1}),
    ({"usage": {"prompt_tokens": 5, "completion_tokens": 2, "note": "free text"}}, {"usage": {"prompt_tokens": 5, "completion_tokens": 2, "note_chars": 9}}),
    ({"tool_calls": [{"tool": "lookup_company", "ok": True, "args": {"q": "Nvidia HBM"}}]},
     {"tool_calls": [{"tool": "lookup_company", "ok": True, "args": {"q_chars": 10}}]}),
    ({"names": ["a b", "c d"]}, {"names_count": 2}),
    ({"question": "hello there"}, {"question_chars": 11, "question_hash": "<hash>"}),
])
def test_scrub_attrs_keeps_numbers_and_labels_and_reduces_everything_else_to_a_length(attrs, expected):
    out = tracing.scrub_attrs(attrs, hasher=lambda text: "<hash>")
    assert out == expected


def test_scrub_attrs_is_idempotent_so_the_masking_hook_can_run_over_its_own_output():
    once = tracing.scrub_attrs({"question": QUESTION, "tool": "t", "n": 1, "usage": {"prompt_tokens": 3}}, hasher=lambda t: "ab" * 8)
    assert tracing.scrub_attrs(once, hasher=lambda t: "cd" * 8) == once


def test_scrub_attrs_is_bounded_on_depth_and_width():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    wide = {f"k{i}": i for i in range(500)}
    assert "f" not in json.dumps(tracing.scrub_attrs(deep))
    assert len(tracing.scrub_attrs(wide)) <= tracing.MAX_ATTRS


# ---------------------------------------------------------------- the caller's exceptions are never swallowed

def test_a_span_never_swallows_the_exception_of_its_body_and_records_only_its_type(langfuse):
    tracer, client = tracer_and_client()
    request = tracer.for_request("q")
    with pytest.raises(ValueError, match="boom"):
        with request.span("tool", tool="t"):
            raise ValueError(f"boom {ANSWER}")
    request.close()
    span = client.roots[0].children[0]
    assert metadata_of(span)["error_type"] == "ValueError" and ANSWER not in sent(client)
    assert any(u.get("level") == "ERROR" for u in span.updates) and span.ended


def test_closing_the_generator_that_holds_an_open_span_propagates_generatorexit(langfuse):
    """A client disconnect closes the agent generator in the middle of a span."""
    tracer, client = tracer_and_client()
    request = tracer.for_request("q")

    def agent():
        with request.span("agent"):
            yield "step"
            yield "never reached"

    gen = agent()
    assert next(gen) == "step"
    gen.close()                                                              # must not raise, must not be swallowed
    with pytest.raises(StopIteration):
        next(gen)
    request.close()
    assert client.roots[0].children[0].ended and metadata_of(client.roots[0].children[0]).get("cancelled") is True


# ---------------------------------------------------------------- fail-open: tracing can never break or slow an answer

@pytest.mark.parametrize("failing", ["start_observation", "update", "end"])
def test_a_failing_client_degrades_to_a_no_op_and_the_caller_never_sees_it(langfuse, caplog, failing):
    tracer, client = tracer_and_client()
    FakeLangfuse.fail_on = {failing}
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.tracing"):
        request = tracer.for_request(QUESTION, strategy="agent")
        with request.span("agent", tool="t") as span:
            span.set(n=1)
            request.event("step", tool="t")
            request.generation(name="planner", model="m", usage={"prompt_tokens": 1}, cost_usd=0.1, input_chars=1, output_chars=1)
        request.flush()
        request.close()
        again = tracer.for_request("another question")                       # a dead tracer hands out the no-op
        with again.span("agent"):
            again.event("step")
        again.close()
    assert len([r for r in caplog.records if "tracing" in r.getMessage()]) == 1     # logged ONCE, however many calls failed
    assert tracer.enabled is False and again is tracing.NULL_TRACER


def test_two_failures_from_two_threads_log_exactly_one_record(langfuse, caplog):
    tracer, client = tracer_and_client()
    FakeLangfuse.fail_on = {"start_observation"}
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.tracing"):
        threads = [threading.Thread(target=lambda: tracer.for_request("q").close()) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len([r for r in caplog.records if "tracing" in r.getMessage()]) == 1


def test_a_failed_import_returns_the_no_op_and_logs_once_without_the_keys(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "langfuse", None)
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.tracing"):
        tracer = tracing.get_tracer(settings())
    assert tracer.enabled is False
    (record,) = [r for r in caplog.records if "tracing" in r.getMessage()]
    assert SECRET not in record.getMessage() and PUBLIC not in record.getMessage()


def test_a_client_that_fails_to_build_returns_the_no_op_and_the_log_redacts_the_keys(langfuse, caplog):
    def boom(**kwargs):
        raise RuntimeError(f"cannot reach {HOST} with {SECRET} / {PUBLIC}")
    langfuse.Langfuse = boom
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.tracing"):
        tracer = tracing.get_tracer(settings())
    assert tracer.enabled is False
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "cannot reach" in text and SECRET not in text and PUBLIC not in text and "***" in text


def test_a_request_level_flush_never_blocks_and_never_calls_the_client(langfuse):
    """The SDK batches in a background thread: an answer must not wait for the network at its own end."""
    tracer, client = tracer_and_client()
    FakeLangfuse.flush_gate = threading.Event()                              # a flush would hang until released
    request = tracer.for_request("q")
    started = time.monotonic()
    request.flush()
    request.close()
    assert time.monotonic() - started < 1.0 and client.flush_calls == 0
    FakeLangfuse.flush_gate.set()


def test_shutdown_flushes_and_closes_the_client_and_never_raises(langfuse):
    tracer, client = tracer_and_client()
    tracer.shutdown()
    assert client.flush_calls == 1 and client.shutdown_calls == 1
    FakeLangfuse.fail_on = {"flush", "shutdown"}
    tracer.shutdown()                                                        # a failing client at teardown must not break the lifespan


def test_importing_the_module_never_imports_langfuse():
    """The lifespan imports the module on every start; only ``get_tracer`` with all three settings may pull the package in."""
    code = "import sys; import semigraph.serve.tracing; assert 'langfuse' not in sys.modules"
    src = str(Path(tracing.__file__).resolve().parents[2])
    proc = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": src}, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


def test_parallel_tool_threads_never_corrupt_the_span_stack_or_raise(langfuse):
    """A graph may run tool calls on threads: the stack is locked, so the worst case is a mis-nested span, never an error."""
    tracer, client = tracer_and_client()
    request = tracer.for_request("q")
    errors: list[BaseException] = []

    def tool_call(i):
        try:
            for _ in range(25):
                with request.span("tool", tool=f"t{i}"):
                    request.event("step", n=i)
                    request.generation(name="planner", model="m", usage={"prompt_tokens": 1}, cost_usd=0.0, input_chars=1, output_chars=1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=tool_call, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    request.close()
    assert errors == [] and tracer.enabled is True
    assert all(o.ended for o in client.everything) and len([o for o in client.everything if o.kwargs["name"] == "tool"]) == 150

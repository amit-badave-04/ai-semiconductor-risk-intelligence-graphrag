"""Pure-logic tests for the service helpers and the streaming answerer —
litellm fully mocked, no Neo4j, no network."""

from types import SimpleNamespace

import litellm
import pytest

import semigraph.retrieval.answerer as answerer_mod
from semigraph.retrieval.answerer import TextStream, answer_stream, usage_cost
from semigraph.serve import store
from semigraph.serve.guard import RateLimiter, ip_hash, validate_question

CID = "0001045810-26-000021:I.1:0320"


# --- guard ---

def test_rate_limiter_allows_up_to_max_then_blocks_and_recovers(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("semigraph.serve.guard.time.monotonic", lambda: now[0])
    rl = RateLimiter(max_events=2, window_seconds=60)
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")  # independent key
    now[0] += 61
    assert rl.allow("a")  # window slid


def test_rate_limiter_disabled_when_max_is_zero():
    assert all(RateLimiter(0, 60).allow("x") for _ in range(50))


def test_ip_hash_is_stable_and_not_the_ip():
    assert ip_hash("203.0.113.9") == ip_hash("203.0.113.9")
    assert "203" not in ip_hash("203.0.113.9") and len(ip_hash("203.0.113.9")) == 16


def test_validate_question_normalizes_whitespace_and_bounds():
    assert validate_question("  what   about\nNvidia? ", 100) == "what about Nvidia?"
    with pytest.raises(Exception):
        validate_question("hi", 100)
    with pytest.raises(Exception):
        validate_question("x" * 101, 100)


# --- store ---

def test_cache_key_ignores_case_spacing_and_trailing_punctuation():
    a = store.cache_key("What about  Nvidia?", "hybrid")
    assert a == store.cache_key("what about nvidia", "hybrid")
    assert a != store.cache_key("what about nvidia", "vector")


# --- usage / cost ---

def test_usage_cost_uses_configured_list_prices():
    assert usage_cost({"prompt_tokens": 1_000_000, "completion_tokens": 0}) == pytest.approx(2.0)
    assert usage_cost({"prompt_tokens": 0, "completion_tokens": 1_000_000}) == pytest.approx(10.0)
    assert usage_cost(None) is None


# --- TextStream (litellm mocked) ---

def chunk(text=None, finish=None, usage=None):
    c = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text),
                                                 finish_reason=finish)])
    c.usage = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None
    return c


def test_textstream_yields_deltas_and_captures_provider_usage(monkeypatch):
    calls = []

    def fake_completion(**kw):
        calls.append(kw)
        return iter([chunk("Hello "), chunk("world"), chunk(None, "stop", (12, 3))])

    monkeypatch.setattr(answerer_mod, "completion", fake_completion)
    s = TextStream("prompt", model="anthropic/claude-sonnet-5", max_tokens=50, timeout=7)
    assert "".join(s) == "Hello world"
    assert s.finish_reason == "stop" and s.usage == {"prompt_tokens": 12, "completion_tokens": 3}
    kw = calls[0]
    assert kw["stream"] is True and kw["thinking"] == {"type": "disabled"} and kw["timeout"] == 7
    assert "temperature" not in kw and "top_p" not in kw


def test_textstream_retries_transient_error_before_first_token(monkeypatch):
    attempts = []

    def fake_completion(**kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise litellm.RateLimitError("slow down", llm_provider="anthropic", model="m")
        return iter([chunk("ok", "stop")])

    monkeypatch.setattr(answerer_mod, "completion", fake_completion)
    monkeypatch.setattr(answerer_mod.time, "sleep", lambda s: None)
    assert "".join(TextStream("p", attempts=2, backoff=(0, 0))) == "ok" and len(attempts) == 2


def test_textstream_raises_when_interrupted_mid_answer(monkeypatch):
    def gen():
        yield chunk("partial ")
        raise litellm.ServiceUnavailableError("overloaded", llm_provider="anthropic", model="m")

    monkeypatch.setattr(answerer_mod, "completion", lambda **kw: gen())
    s = TextStream("p", attempts=2, backoff=(0, 0))
    with pytest.raises(RuntimeError, match="interrupted"):
        list(s)


def test_textstream_gives_up_after_attempts_on_empty(monkeypatch):
    monkeypatch.setattr(answerer_mod, "completion", lambda **kw: iter([chunk(None, "stop")]))
    with pytest.raises(RuntimeError, match="after 2 attempts"):
        list(TextStream("p", attempts=2, backoff=(0, 0)))


# --- answer_stream (retrieval mocked) ---

class _StreamWithAttrs:
    def __init__(self, gen, usage, finish_reason):
        self._gen, self.usage, self.finish_reason = gen, usage, finish_reason

    def __iter__(self):
        return self._gen


def test_answer_stream_events_and_citation_verification(monkeypatch):
    retrieval = {"anchors": {"Nvidia": 1045810}, "edges": [], "metrics": [], "risks": [],
                 "temporal": [],
                 "chunks": [{"chunk_id": CID, "score": 0.9, "text": "HBM text", "source_url": "u"}]}
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: retrieval)
    bogus = "0000000000-00-000000:I.1:9999"

    def llm_stream(prompt):
        assert "HBM text" in prompt
        yield f"Nvidia depends on SK hynix [{CID}] and [{bogus}]."

    events = list(answer_stream(
        "q", driver=None, embedder=None,
        llm_stream=lambda p: _StreamWithAttrs(llm_stream(p), {"prompt_tokens": 100, "completion_tokens": 10}, "stop")))
    assert [e["event"] for e in events] == ["retrieval", "delta", "done"]
    done = events[-1]
    assert done["citations"] == sorted([CID, bogus])
    assert done["hallucinated"] == [bogus]
    assert done["usage"]["prompt_tokens"] == 100 and done["cost_usd"] == pytest.approx(0.0003)


def test_answer_stream_rejects_unknown_strategy():
    with pytest.raises(ValueError):
        list(answer_stream("q", None, None, strategy="nope"))


def test_answer_stream_mid_stream_failure_yields_error_with_usage(monkeypatch):
    retrieval = {"anchors": {}, "edges": [], "metrics": [], "risks": [], "temporal": [], "chunks": []}
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: retrieval)

    class Broken:
        usage = {"prompt_tokens": 40, "completion_tokens": 3}
        finish_reason = None

        def __iter__(self):
            yield "part"
            raise RuntimeError("stream interrupted mid-answer: ServiceUnavailableError")

    events = list(answer_stream("q", None, None, llm_stream=lambda p: Broken()))
    assert [e["event"] for e in events] == ["retrieval", "delta", "error"]
    assert events[-1]["partial"] == "part" and events[-1]["usage"]["prompt_tokens"] == 40
    assert events[-1]["cost_usd"] == pytest.approx(40 * 2 / 1e6 + 3 * 10 / 1e6)

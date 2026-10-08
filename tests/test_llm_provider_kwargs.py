"""The staging switch of the model calls: ``OPENAI_API_BASE`` (M5a I5, docs/v2/M5_DECISIONS.md 2.1 "Staging rules").

The staging API runs the mock LLM (``tools/mockllm``) instead of a provider. Its three model strings are ``openai/mock-*``
and every call of the serve path must be sent to ``settings.openai_api_base`` (the mock's private name), never to a
provider. What is pinned here:

* ``llm_shape.provider_kwargs(model, settings)`` is ``{"api_base": base}`` for an ``openai/`` model when a base is set, and
  ``{}`` for everything else (no base, another provider, a settings object that has no such attribute);
* a RECORDING fake of the three entry points proves that every call carries ``api_base`` when it is set and none when it is
  not: the sync writer (``llm_text`` and ``TextStream``), the async stream (a whole escalating ``aanswer_stream``: the draft
  call AND the strong call) and the planner;
* a source scan proves that no call to ``completion`` / ``acompletion`` in ``retrieval/``, ``agent/`` or ``serve/`` bypasses
  ``provider_kwargs``, so a new call site cannot be added without the switch;
* ``openai/mock-*`` is priced as the model it stands for (``usage_cost``), and ``serve.estimate`` shares the one alias table
  with ``llm_shape``.

No network, no model: every model call is a recording fake. Runs in the serve-shipped CI job.
"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from agent_fakes import FakeDriver, FakeEmbedder

from semigraph import llm_shape
from semigraph.agent import planner as planner_mod
from semigraph.config import Settings
from semigraph.retrieval import answerer, answerer_async
from semigraph.retrieval.answerer_async import AsyncTextStream
from semigraph.serve import estimate
from semigraph.serve.limiters import make_limiters

SRC = Path(__file__).resolve().parent.parent / "src" / "semigraph"
BASE = "http://semigraph-mockllm.internal:8000/v1"
DRAFT, STRONG = "openai/mock-luna", "openai/mock-sonnet"
HARD_TIMEOUT_S = 20
USAGE = {"prompt_tokens": 12_345, "completion_tokens": 678}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """A variable left in the developer's shell must neither set nor clear the base these tests build settings with."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def settings_with(base: str) -> Settings:
    return Settings(_env_file=None, openai_api_base=base)


def run(scenario):
    async def guarded():
        with anyio.fail_after(HARD_TIMEOUT_S):
            return await scenario()
    return asyncio.run(guarded())


def assert_every_call_carries(calls: list[dict], base: str) -> None:
    assert calls, "the fake saw no call at all"
    for call in calls:
        if base:
            assert call.get("api_base") == base
        else:
            assert "api_base" not in call


# ---------------------------------------------------------------- provider_kwargs

@pytest.mark.parametrize("model", [DRAFT, STRONG, "openai/mock-haiku", "openai/gpt-6-luna"])
def test_an_openai_model_gets_the_base_when_one_is_set(model):
    assert llm_shape.provider_kwargs(model, SimpleNamespace(openai_api_base=BASE)) == {"api_base": BASE}


@pytest.mark.parametrize("model", ["anthropic/claude-sonnet-5", "gemini/x", "deepinfra/x", "together_ai/x", "gpt-6", ""])
def test_a_model_of_another_provider_never_gets_one(model):
    assert llm_shape.provider_kwargs(model, SimpleNamespace(openai_api_base=BASE)) == {}


@pytest.mark.parametrize("settings", [SimpleNamespace(openai_api_base=""), SimpleNamespace(openai_api_base="   "),
                                      SimpleNamespace(openai_api_base=None), SimpleNamespace(), None],
                         ids=["empty", "blank", "none", "no attribute", "no settings"])
def test_no_base_means_no_argument_so_a_call_is_exactly_what_it_was(settings):
    """A settings stand-in of another test (no such attribute) must keep working, and a missing base must never turn into
    ``api_base=None`` or an empty string (litellm would then use it)."""
    assert llm_shape.provider_kwargs(DRAFT, settings) == {}


def test_the_base_is_stripped_and_each_call_gets_a_fresh_dict():
    settings = SimpleNamespace(openai_api_base=f"  {BASE}\n")
    first = llm_shape.provider_kwargs(DRAFT, settings)
    assert first == {"api_base": BASE}
    first["api_base"] = "changed"
    assert llm_shape.provider_kwargs(DRAFT, settings) == {"api_base": BASE}


def test_the_real_settings_carry_the_base_and_it_defaults_to_none():
    assert Settings(_env_file=None).openai_api_base == ""
    assert llm_shape.provider_kwargs(DRAFT, Settings(_env_file=None)) == {}
    assert llm_shape.provider_kwargs(DRAFT, settings_with(BASE)) == {"api_base": BASE}


def test_the_base_is_read_from_its_environment_variable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_BASE", BASE)
    assert Settings(_env_file=None).openai_api_base == BASE


# ---------------------------------------------------------------- the sync writer

def reply(text: str = "hello"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")])


def stream_chunks():
    return [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello world"), finish_reason="stop")],
                            usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2))]


@pytest.mark.parametrize("base", ["", BASE])
def test_the_sync_text_call_carries_the_base_only_when_one_is_set(monkeypatch, base):
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return reply()

    monkeypatch.setattr(answerer, "completion", completion)
    monkeypatch.setattr(answerer, "get_settings", lambda: settings_with(base))
    assert answerer.llm_text("PROMPT", model=DRAFT) == "hello"
    assert_every_call_carries(calls, base)


@pytest.mark.parametrize("base", ["", BASE])
def test_the_sync_stream_carries_the_base_only_when_one_is_set(monkeypatch, base):
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return iter(stream_chunks())

    monkeypatch.setattr(answerer, "completion", completion)
    monkeypatch.setattr(answerer, "get_settings", lambda: settings_with(base))
    assert "".join(answerer.TextStream("PROMPT", model=STRONG)) == "Hello world"
    assert_every_call_carries(calls, base)


# ---------------------------------------------------------------- the async stream, whole

class Upstream:
    """What ``acompletion(stream=True)`` returns: an async iterator with ``aclose``."""

    def __init__(self, items):
        self.items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.items:
            raise StopAsyncIteration
        return self.items.pop(0)

    async def aclose(self):
        return None


class RecordingAcompletion:
    def __init__(self, *upstreams):
        self.upstreams, self.calls = list(upstreams), []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.upstreams.pop(0)


@pytest.mark.parametrize("base", ["", BASE])
def test_the_async_stream_carries_the_base_only_when_one_is_set(monkeypatch, base):
    fake = RecordingAcompletion(Upstream(stream_chunks()))
    monkeypatch.setattr(answerer_async, "acompletion", fake)
    monkeypatch.setattr(answerer_async, "get_settings", lambda: settings_with(base))

    async def go():
        return [delta async for delta in AsyncTextStream("PROMPT", model=DRAFT)]

    assert run(go) == ["Hello world"]
    assert_every_call_carries(fake.calls, base)


@pytest.mark.parametrize("base", ["", BASE])
def test_an_escalating_answer_sends_both_the_draft_and_the_strong_call_to_the_base(monkeypatch, base):
    """The whole path: retrieval, the draft (it cites nothing, so the checks reject it), the escalation to the strong
    model. Two paid calls; both must go where the settings say."""
    draft = Upstream([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Nvidia revenue was $215.9 billion."),
                                                               finish_reason="stop")], usage=None),
                      SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=900, completion_tokens=20))])
    strong = Upstream(stream_chunks())
    fake = RecordingAcompletion(draft, strong)
    monkeypatch.setattr(answerer_async, "acompletion", fake)
    monkeypatch.setattr(answerer_async, "get_settings", lambda: settings_with(base))
    limiters = make_limiters(SimpleNamespace(embed_slots=1, db_thread_limit=4))

    async def go():
        stream = answerer_async.aanswer_stream("Who supplies Nvidia's HBM?", FakeDriver.world(), FakeEmbedder(),
                                               limiters=limiters, model=DRAFT, escalation_model=STRONG, timeout=90,
                                               max_tokens=2400)
        try:
            return [event async for event in stream]
        finally:
            await stream.aclose()

    events = run(go)
    assert "escalated" in [event["event"] for event in events] and events[-1]["event"] == "done"
    assert [call["model"] for call in fake.calls] == [DRAFT, STRONG]
    assert_every_call_carries(fake.calls, base)


# ---------------------------------------------------------------- the planner

@pytest.mark.parametrize("base", ["", BASE])
def test_the_planner_call_carries_the_base_only_when_one_is_set(monkeypatch, base):
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None), finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2))

    monkeypatch.setattr(planner_mod.litellm, "completion", completion)
    monkeypatch.setattr(planner_mod, "get_settings", lambda: settings_with(base))
    turn = planner_mod.LiteLLMPlanner(DRAFT)([{"role": "user", "content": "q"}], [], timeout=5.0)
    assert turn.tool_calls == () and turn.usage == {"prompt_tokens": 10, "completion_tokens": 2}
    assert_every_call_carries(calls, base)


# ---------------------------------------------------------------- no call site without the switch

SERVE_PATH_DIRS = ("retrieval", "agent", "serve")
CALLEES = {"completion", "acompletion"}


def model_calls(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if callee in CALLEES:
                yield node


def test_every_model_call_of_the_serve_path_is_given_the_provider_kwargs():
    """Parity by construction: a new ``completion(...)`` in ``retrieval/``, ``agent/`` or ``serve/`` that forgets
    ``**provider_kwargs(...)`` would send a staging call to a provider."""
    seen, bare = 0, []
    for folder in SERVE_PATH_DIRS:
        for path in sorted((SRC / folder).rglob("*.py")):
            for call in model_calls(path):
                seen += 1
                splats = [ast.unparse(keyword.value) for keyword in call.keywords if keyword.arg is None]
                if not any(text.startswith("provider_kwargs(") for text in splats):
                    bare.append(f"{path.relative_to(SRC)}:{call.lineno}")
    assert seen >= 4                   # answerer: llm_text + TextStream; answerer_async: the stream; planner: the planner
    assert bare == []


# ---------------------------------------------------------------- prices

def test_the_alias_table_lives_in_llm_shape_and_the_estimate_shares_it():
    assert llm_shape.MOCK_MODEL_PREFIX == "openai/mock-"
    assert set(llm_shape.MOCK_ALIASES) == {"openai/mock-luna", "openai/mock-sonnet", "openai/mock-haiku"}
    assert all(real in llm_shape.KNOWN_PRICES_PER_MTOK for real in llm_shape.MOCK_ALIASES.values())
    assert estimate.MOCK_ALIASES is llm_shape.MOCK_ALIASES and estimate.MOCK_MODEL_PREFIX == llm_shape.MOCK_MODEL_PREFIX


@pytest.mark.parametrize("mock,real", sorted(llm_shape.MOCK_ALIASES.items()))
def test_a_mock_model_costs_what_the_model_it_stands_for_costs(monkeypatch, mock, real):
    """Staging prices a run as the live models would be priced: the settled spend must reach the live caps' arithmetic."""
    consulted = []

    def price_map(**kwargs):
        consulted.append(kwargs["model"])         # usage_cost swallows an exception raised here, so record the call instead
        raise ValueError("This model isn't mapped yet")

    monkeypatch.setattr(answerer.litellm, "cost_per_token", price_map)
    cost = answerer.usage_cost(USAGE, mock)
    per_in, per_out = llm_shape.KNOWN_PRICES_PER_MTOK[real]
    assert cost == answerer.usage_cost(USAGE, real) == round(12_345 * per_in / 1e6 + 678 * per_out / 1e6, 6)
    assert cost > 0 and consulted == []           # a model with a listed price never consults litellm's price map


def test_a_mock_that_stands_for_no_listed_model_gets_the_configured_prices_not_zero(monkeypatch):
    def unknown(**kwargs):
        raise ValueError("This model isn't mapped yet")

    monkeypatch.setattr(answerer.litellm, "cost_per_token", unknown)
    monkeypatch.setattr(answerer, "get_settings", lambda: Settings(_env_file=None))
    assert answerer.usage_cost(USAGE, "openai/mock-unlisted") == round(12_345 * 2.0 / 1e6 + 678 * 10.0 / 1e6, 6)

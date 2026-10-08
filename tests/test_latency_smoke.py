"""scripts/latency_smoke.py: the S12 calibration runner (docs/v2/M5_PLAN.md section 5, window W2).

The script spends money only under ``--live``; nothing here ever passes it. What is proved offline:

* ``--dry-run`` prints the plan and a cost bound that never exceeds ``--max-usd``, and calls NOTHING: LiteLLM (every entry
  point the answer path imports by name), sockets, ``.env`` loading and reading, the Neo4j driver and the embedder all fail
  the test if touched;
* the cost guard: asks are started only while ``spent + worst case <= max-usd``, unknown costs are charged at the worst
  case, a crash or a timeout is recorded without its message, and over thousands of random runs the spend never crosses the cap;
* the worst-case arithmetic is the service's own (``serve/estimate``), pinned at the estimate's prompt ceiling;
* a full rehearsal of the live code path ($0): the real ``LiveRuntime`` (retrieval hop, prompt, timed model streams, escalation,
  the agent twin with its planner) against the mock LLM on loopback and the fake graph, whose output the mock's
  calibration accepts.
"""

import asyncio
import builtins
import contextlib
import importlib.util
import json
import logging
import random
import re
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_fakes import FakeDriver, FakeEmbedder
from mockllm_fixtures import LiveMock, make_app, metrics_of   # also sets the offline cost-map flag and the import path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "latency_smoke.py"
spec = importlib.util.spec_from_file_location("latency_smoke_script", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
sys.modules["latency_smoke_script"] = smoke
spec.loader.exec_module(smoke)

from tools.mockllm import calibrate  # noqa: E402
from tools.mockllm.profile import load_profile  # noqa: E402

SECRET_QUESTION_MARKER = "QUESTION-TEXT-MARKER"

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


@contextlib.contextmanager
def logging_state_kept(names=UVICORN_LOGGERS):
    """Restores the named loggers' level, handlers, propagation and disabled flag on exit.

    ``LiveMock`` builds ``uvicorn.Config(log_level="error")``, which sets ``uvicorn.error`` to ERROR, installs handlers on
    ``uvicorn`` / ``uvicorn.access`` and stops ``uvicorn`` propagating, for the whole process. Every later test that listens on
    ``uvicorn.error`` (the drain's warnings, ``tests/test_serve_drain.py``) then silently receives nothing. The state is global,
    so the only safe place to undo it is here, around every test of this file.
    """
    saved = {}
    for name in names:
        lg = logging.getLogger(name)
        saved[name] = (lg.level, list(lg.handlers), lg.propagate, lg.disabled, list(lg.filters))
    try:
        yield
    finally:
        for name, (level, handlers, propagate, disabled, filters) in saved.items():
            lg = logging.getLogger(name)
            lg.setLevel(level)
            lg.handlers[:] = handlers
            lg.propagate, lg.disabled = propagate, disabled
            lg.filters[:] = filters


@pytest.fixture(autouse=True)
def no_logging_state_leaks():
    with logging_state_kept():
        yield


def boom(*args, **kwargs):
    raise AssertionError("the dry run reached for the outside world")


@pytest.fixture
def sealed_off(monkeypatch):
    """Everything that could spend money or touch a service raises; opening a ``.env`` raises too."""
    import dotenv
    import litellm
    import neo4j

    import semigraph.embeddings
    import semigraph.graph.client
    import semigraph.retrieval.answerer
    import semigraph.retrieval.answerer_async
    for target in (litellm, semigraph.retrieval.answerer_async):
        monkeypatch.setattr(target, "acompletion", boom)
    monkeypatch.setattr(litellm, "completion", boom)
    monkeypatch.setattr(semigraph.retrieval.answerer, "completion", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(dotenv, "load_dotenv", boom)
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", boom)
    monkeypatch.setattr(semigraph.graph.client, "get_driver", boom)
    monkeypatch.setattr(semigraph.embeddings, "Embedder", boom)
    real_open = builtins.open

    def guarded_open(file, *a, **k):
        if Path(str(file)).name.startswith(".env"):
            raise AssertionError("the dry run opened a .env file")
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "read_text", _guard_read_text(Path.read_text))


def _guard_read_text(real):
    def read_text(self, *a, **k):
        if self.name.startswith(".env"):
            raise AssertionError("the dry run read a .env file")
        return real(self, *a, **k)
    return read_text


def run_dry(capsys, *extra: str) -> tuple[int, str]:
    code = smoke.main(["--dry-run", *extra])
    return code, capsys.readouterr().out


def bound_of(out: str) -> float:
    return float(re.search(r"COST BOUND \(hard\): \$([0-9.]+) <= --max-usd", out).group(1))


def uvicorn_logging_state():
    return {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers), logging.getLogger(n).propagate,
                logging.getLogger(n).disabled) for n in UVICORN_LOGGERS}


def test_a_live_mock_leaves_no_uvicorn_logging_state_behind():
    """The regression behind tests/test_serve_drain.py failing after this file: LiveMock lowers ``uvicorn.error`` to ERROR."""
    for name in UVICORN_LOGGERS:                          # a known baseline, whatever an earlier test file left behind
        lg = logging.getLogger(name)
        lg.setLevel(logging.NOTSET)
        lg.handlers[:] = []
        lg.propagate, lg.disabled = True, False
    baseline = uvicorn_logging_state()

    with logging_state_kept():
        with LiveMock(make_app()):
            assert logging.getLogger("uvicorn.error").level == logging.ERROR          # the leak this guards against
    assert uvicorn_logging_state() == baseline

    seen = []
    handler = logging.Handler()
    handler.emit = lambda record: seen.append(record.getMessage())
    logging.getLogger("uvicorn.error").addHandler(handler)
    try:
        logging.getLogger("uvicorn.error").warning("a warning the drain would log")
    finally:
        logging.getLogger("uvicorn.error").removeHandler(handler)
    assert seen == ["a warning the drain would log"]


# --- the dry run --------------------------------------------------------------------------------------------------------

def test_a_mode_is_required_and_there_is_no_default_that_spends():
    for argv in ([], ["--max-usd", "0.5"]):
        with pytest.raises(SystemExit) as e:
            smoke.parse_args(argv)
        assert e.value.code == 2
    with pytest.raises(SystemExit):
        smoke.parse_args(["--dry-run", "--live"])


@pytest.mark.parametrize("argv", [["--dry-run", "--max-usd", "0"], ["--dry-run", "--max-usd", "-1"],
                                  ["--dry-run", "--max-usd", "nan"], ["--dry-run", "--sec", "0", "--agent", "0"],
                                  ["--dry-run", "--sec", "-1"]])
def test_nonsense_arguments_are_refused(argv):
    with pytest.raises(SystemExit):
        smoke.parse_args(argv)


def test_the_dry_run_prints_the_plan_and_calls_nothing(sealed_off, capsys):
    code, out = run_dry(capsys)
    assert code == 0
    assert "DRY RUN" in out and "nothing was called" in out
    assert "draft=openai/gpt-6-luna" in out and "strong=anthropic/claude-sonnet-5" in out       # the live strings of fly.toml
    plan = smoke.select_asks(smoke.DEFAULT_SEC, smoke.DEFAULT_AGENT)
    assert len(plan) == 12 and all(f" {ask.id} " in out for ask in plan)
    assert "10 SEC" in out and "2 agent" in out


def test_the_seal_is_real_the_live_mode_trips_it_and_each_guarded_door_raises(sealed_off):
    """Negative control: the dry run's silence proves something only if touching the outside world fails under this fixture."""
    import litellm
    with pytest.raises(AssertionError, match="outside world"):
        smoke.main(["--live", "--max-usd", "0.01"])                   # reaches load_dotenv() first
    with pytest.raises(AssertionError):
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(AssertionError):
        asyncio.run(litellm.acompletion(model="openai/gpt-6-luna", messages=[]))
    with pytest.raises(AssertionError, match=".env"):
        open(ROOT / ".env")                                           # noqa: SIM115 - the guard raises before any file is opened
    with pytest.raises(AssertionError, match=".env"):
        (ROOT / ".env.fly").read_text()


@pytest.mark.parametrize("max_usd", [0.01, 0.1, 0.5, 2.0, 50.0])
def test_the_printed_bound_never_exceeds_max_usd(sealed_off, capsys, max_usd):
    code, out = run_dry(capsys, "--max-usd", str(max_usd))
    assert code == 0 and 0 < bound_of(out) <= max_usd
    if max_usd == 0.01:
        assert "at the reference prompts above: 0 of 12" in out and bound_of(out) == 0.01


def test_the_default_cap_is_fifty_cents_and_the_bound_is_that_cap(sealed_off, capsys):
    _, out = run_dry(capsys)
    assert smoke.DEFAULT_MAX_USD == 0.50 and bound_of(out) == 0.5


def test_the_bound_is_the_services_own_ceiling_when_the_cap_would_never_bind(sealed_off, capsys):
    """Not a number made from the recorded prompts: a prompt larger than any recorded one is allowed, so the bound that holds
    is ``serve/estimate``'s ceiling for the ask (its retrieval caps), and the cap when that is lower."""
    from semigraph.serve import estimate as E
    settings = smoke.price_settings(smoke.live_config())
    _, out = run_dry(capsys, "--sec", "1", "--agent", "0", "--max-usd", "5")
    assert bound_of(out) == pytest.approx(E.estimate("hybrid", settings).usd, abs=1e-6)
    assert bound_of(out) < 5
    _, out = run_dry(capsys, "--sec", "1", "--agent", "1", "--max-usd", "50")
    assert bound_of(out) == pytest.approx(E.estimate("hybrid", settings).usd + E.estimate("agent", settings).usd, abs=2e-6)
    _, out = run_dry(capsys, "--sec", "1", "--agent", "0", "--max-usd", "0.2")
    assert bound_of(out) == 0.2


def test_the_dry_run_does_not_print_any_environment_value(sealed_off, capsys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-openai-key-4242")       # gitleaks:allow
    monkeypatch.setenv("NEO4J_PASSWORD", "sentinel-neo4j-password-4242")   # gitleaks:allow
    _, out = run_dry(capsys)
    assert "4242" not in out and "sentinel" not in out


# --- the plan --------------------------------------------------------------------------------------------------------------

def test_the_plan_is_a_deterministic_mix_with_agent_asks_first_and_no_injection_test():
    first, second = smoke.select_asks(10, 2), smoke.select_asks(10, 2)
    assert first == second and len(first) == 12
    assert [a.kind for a in first] == ["agent"] * 2 + ["sec"] * 10
    assert len({a.id for a in first}) == 12 and len({a.qtype for a in first if a.kind == "sec"}) >= 5
    assert not any(a.qtype == "injection" for a in first) and all(a.question.strip() for a in first)
    assert len(smoke.select_asks(3, 0)) == 3 and len(smoke.select_asks(0, 2)) == 2


def test_the_live_models_come_from_fly_toml_and_fall_back_to_the_documented_strings(tmp_path):
    live = smoke.live_config()
    assert live.models.draft == "openai/gpt-6-luna" and live.answer_max_tokens == 2400
    toml = tmp_path / "fly.toml"
    toml.write_text('[env]\n  ANSWER_MODEL = "a/b"\n  ESCALATION_MODEL = ""\n  LLM_ANSWER_MAX_TOKENS = "99"\n', encoding="utf-8")
    custom = smoke.live_config(toml)
    assert (custom.models.draft, custom.models.strong, custom.answer_max_tokens) == ("a/b", "anthropic/claude-sonnet-5", 99)
    assert smoke.live_config(tmp_path / "missing.toml").models.planner == "openai/gpt-6-luna"


def test_only_a_local_graph_is_accepted():
    for uri in ("bolt://localhost:7687", "neo4j://127.0.0.1:7687", "bolt://[::1]:7687"):
        smoke.require_local_graph(SimpleNamespace(neo4j_uri=uri))
    for uri in ("neo4j+s://semigraph-neo4j.internal:7687", "bolt://example.com:7687", ""):
        with pytest.raises(SystemExit, match="LOCAL graph only") as e:
            smoke.require_local_graph(SimpleNamespace(neo4j_uri=uri))
        assert uri not in str(e.value) or uri == ""


# --- the worst case is the service's own arithmetic -----------------------------------------------------------------------------

def test_the_worst_case_equals_the_services_estimate_at_the_estimates_own_prompt_ceiling():
    """``worst_case_usd`` takes the prompt's CHARACTERS and sizes them per model, as the service does (Sonnet 2.0 characters
    per token, Luna 2.5): at the estimate's own ceiling in characters it is the estimate, to the micro-dollar."""
    from semigraph.serve import estimate as E
    settings = smoke.price_settings(smoke.live_config())
    for ask_type, planner in (("hybrid", False), ("vector", False), ("agent", True)):
        ceiling = E.estimate(ask_type, settings)
        chars = E.prompt_chars(ask_type, settings)
        assert smoke.worst_case_usd(chars, settings, planner=planner) == ceiling.usd, ask_type


def _shaped_settings(answer: str, escalation: str, planner: str):
    from semigraph.config import Settings
    return Settings(_env_file=None, answer_model=answer, escalation_model=escalation, agent_planner_model=planner,
                    llm_answer_max_tokens=2400)


@pytest.mark.parametrize("shape", [
    ("openai/gpt-6-luna", "anthropic/claude-sonnet-5", "openai/gpt-6-luna"),        # live: cheap draft, Sonnet escalation
    ("anthropic/claude-sonnet-5", "", "openai/gpt-6-luna"),                          # the rollback: Sonnet alone
    ("anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5", "openai/gpt-6-luna"),  # an escalation equal to the answer model
    ("openai/gpt-6-luna", "", "openai/gpt-6-luna"),                                  # Luna alone
    ("openai/mock-luna", "openai/mock-sonnet", "openai/mock-luna"),                  # staging mocks, priced and sized as the real ones
])
def test_the_worst_case_is_the_services_estimate_whatever_the_model_shape(shape):
    from semigraph.serve import estimate as E
    settings = _shaped_settings(*shape)
    for ask_type, planner in (("hybrid", False), ("vector", False), ("workspace", False), ("agent", True)):
        assert smoke.worst_case_usd(E.prompt_chars(ask_type, settings), settings, planner=planner) == \
            E.estimate(ask_type, settings).usd, (shape, ask_type)


def test_the_worst_case_grows_with_the_prompt_and_with_a_planner():
    settings = smoke.price_settings(smoke.live_config())
    small, large = smoke.worst_case_usd(5_000, settings), smoke.worst_case_usd(50_000, settings)
    assert 0 < small < large and smoke.worst_case_usd(5_000, settings, planner=True) > small


def test_the_helpers_the_live_run_depends_on_still_exist():
    from semigraph.retrieval.answerer import _draft_kwargs
    from semigraph.serve import estimate as E
    assert _draft_kwargs({"timeout": 90})["attempts"] == 1 and callable(E._planner_component)
    assert callable(E._answer_components) and callable(E._component) and callable(E.tokens_for_chars)
    assert hasattr(E, "DRAFT_ATTEMPTS") and hasattr(E, "STREAM_ATTEMPTS") and hasattr(E, "chars_per_token")
    assert not hasattr(E, "CHARS_PER_TOKEN")           # the single constant is gone: this script sizes per model


# --- the per-call guard sizes each model's prompt at ITS characters per token ------------------------------------------------

def test_a_sonnet_calls_guard_prices_its_prompt_at_two_characters_per_token_and_a_lunas_at_two_and_a_half():
    """100,000 characters of prompt. Sonnet: 50,000 tokens in at $2 per million, 2,400 out at $10, two attempts (the stream's
    own retry of an empty reply) = 2 x (100,000 + 24,000) = 248,000 micro-dollars. Luna: 40,000 tokens in at $0.10, 2,400 out
    at $0.50, one attempt = 4,000 + 1,200 = 5,200. At the old single 2.5 the Sonnet call was bounded at 208,000: 16% low."""
    ps = smoke.price_settings(smoke.live_config())
    assert smoke.call_worst_case_usd("strong", 100_000, ps) == 0.248
    assert smoke.call_worst_case_usd("draft", 100_000, ps) == 0.0052
    assert smoke.call_worst_case_usd("strong", 100_001, ps) > 0.248            # 50,001 tokens: the characters round UP
    mock = smoke.price_settings(smoke.LiveConfig(smoke.Models("openai/mock-luna", "openai/mock-sonnet", "openai/mock-luna"), 2400))
    assert smoke.call_worst_case_usd("strong", 100_000, mock) == 0.248         # a mock is sized and priced as the model it stands for
    assert smoke.call_worst_case_usd("draft", 100_000, mock) == 0.0052


def test_the_live_runs_factories_commit_a_sonnet_calls_worst_case_at_two_characters_per_token():
    """The guard that decides, before any request leaves the process, whether a call fits under the cap: the real factories
    of ``LiveRuntime`` (no model is called: the stream is built, never iterated)."""
    from semigraph.config import Settings
    from semigraph.serve.limiters import make_limiters
    settings = Settings(_env_file=None)
    runtime = smoke.LiveRuntime(FakeDriver.world(), FakeEmbedder(), make_limiters(settings), settings, smoke.live_config())
    budget = smoke.Budget(5.0)
    draft, strong = runtime._factories([[]], budget)
    strong("x" * 100_000)
    assert budget.pending == pytest.approx(0.248) and budget.calls == 1
    draft("x" * 100_000)
    assert budget.pending == pytest.approx(0.248 + 0.0052) and budget.calls == 2


def test_a_reference_prompt_in_tokens_is_read_on_the_densest_model_so_sonnet_is_priced_at_exactly_those_tokens():
    """The recorded prompt sizes and ``--agent-prompt-tokens`` are BILLED TOKENS, not characters. They are turned into
    characters at the densest ratio the service assumes (Sonnet's 2.0) before the per-model sizing, so the dearest model's
    component is priced at exactly the stated tokens and Luna's at 0.8 of them (Luna reads the same prompt as fewer tokens).
    60,000 tokens + the planner: Sonnet 2 x (60,000 x 2 + 24,000) = 288,000; Luna 48,000 x 0.10 + 1,200 = 6,000; planner
    3 x (23,400 x 0.10 + 400 x 0.50) = 7,620; 301,620 micro-dollars."""
    ps = smoke.price_settings(smoke.live_config())
    assert smoke.reference_chars(60_000) == 120_000 and smoke.reference_chars(1) == 2
    assert smoke.reference_worst_case_usd(60_000, ps, planner=True) == 0.30162
    assert smoke.reference_worst_case_usd(60_000, ps) == 0.294


# --- the guard, the clock and the record (fake asks, no model) -----------------------------------------------------------------

def fake_prepared(reserve: float, cost: float | None, *, terminal: str = "done", routed: str = "cheap", escalated: bool = False,
                  delay: float = 0.0, calls=(), crash: str | None = None):
    async def events(sink):
        sink.extend(calls)
        if crash:
            raise RuntimeError(crash)
        yield {"event": "delta", "text": "x"}
        if delay:
            await asyncio.sleep(delay)
        yield {"event": terminal, "cost_usd": cost, "routed": routed, "escalated": escalated,
               "escalation_reasons": ["invalid_citation"] if escalated else None,
               "checks": {"citations_retrieved": True, "numbers_grounded": True}}
    return smoke.Prepared(reserve, 5000, 0.3, 0.2, events)


def plan_of(n: int, max_usd: float, timeout: float = 5.0):
    asks = tuple(smoke.PlannedAsk(f"Q{i}", "sec", "numeric", f"{SECRET_QUESTION_MARKER} {i}") for i in range(n))
    return smoke.Plan(asks, max_usd, ask_timeout_s=timeout)


def run(plan, prepared_by_id):
    async def prepare(ask, budget):
        item = prepared_by_id[ask.id]
        if isinstance(item, Exception):
            raise item
        return item
    return asyncio.run(smoke.run_plan(plan, prepare))


def test_an_ask_runs_only_while_its_worst_case_fits_next_to_what_is_spent():
    doc = run(plan_of(4, 0.30), {"Q0": fake_prepared(0.20, 0.05), "Q1": fake_prepared(0.20, 0.05),
                                 "Q2": fake_prepared(0.20, 0.05), "Q3": fake_prepared(0.31, 0.01)})
    statuses = {a["id"]: a["status"] for a in doc["asks"]}
    assert statuses == {"Q0": "ran", "Q1": "ran", "Q2": "skipped_cost_guard", "Q3": "skipped_cost_guard"}
    assert doc["summary"]["spent_usd"] == pytest.approx(0.10) and doc["summary"]["asks_skipped"] == 2


def test_an_ask_that_reports_no_cost_is_charged_its_worst_case():
    doc = run(plan_of(2, 0.50), {"Q0": fake_prepared(0.40, None), "Q1": fake_prepared(0.20, 0.01)})
    assert doc["asks"][0]["charged_usd"] == 0.40 and doc["asks"][1]["status"] == "skipped_cost_guard"
    assert doc["summary"]["spent_usd"] == pytest.approx(0.40)


def test_a_stream_error_event_counts_its_cost_and_is_recorded():
    doc = run(plan_of(1, 0.5), {"Q0": fake_prepared(0.1, 0.03, terminal="error")})
    assert doc["asks"][0]["terminal"] == "error" and doc["asks"][0]["failure"] == "stream error event"
    assert doc["summary"]["spent_usd"] == pytest.approx(0.03)


def test_a_crash_is_recorded_by_class_only_and_charged_the_worst_case():
    doc = run(plan_of(2, 0.5), {"Q0": fake_prepared(0.1, 0.0, crash="provider said: sk-live-SECRET-9999"),   # gitleaks:allow
                                "Q1": fake_prepared(0.1, 0.02)})
    assert doc["asks"][0]["terminal"] == "failed" and doc["asks"][0]["failure"] == "RuntimeError"
    assert "SECRET" not in json.dumps(doc) and doc["asks"][1]["status"] == "ran"
    assert doc["summary"]["spent_usd"] == pytest.approx(0.1 + 0.02)


def test_a_slow_ask_times_out_and_is_charged_its_worst_case():
    doc = run(plan_of(2, 0.5, timeout=0.05), {"Q0": fake_prepared(0.1, 0.0, delay=2.0), "Q1": fake_prepared(0.1, 0.02)})
    assert doc["asks"][0]["terminal"] == "timeout" and doc["asks"][0]["charged_usd"] == 0.1
    assert doc["asks"][1]["status"] == "ran"


def test_a_failed_retrieval_buys_nothing_and_does_not_stop_the_run():
    doc = run(plan_of(2, 0.5), {"Q0": ConnectionError("graph down"), "Q1": fake_prepared(0.1, 0.02)})
    assert doc["asks"][0] == {"id": "Q0", "kind": "sec", "qtype": "numeric", "status": "preparation_failed",
                              "failure": "ConnectionError", "charged_usd": 0.0}
    assert doc["summary"]["asks_run"] == 1


def test_the_spend_never_crosses_the_cap_when_no_ask_costs_more_than_its_worst_case():
    rng = random.Random(2026)
    for _ in range(300):
        cap = rng.uniform(0.05, 0.6)
        items = {f"Q{i}": fake_prepared(rng.uniform(0.01, 0.3), None) for i in range(8)}
        items = {k: fake_prepared(v.reserve_usd, rng.uniform(0, v.reserve_usd), escalated=rng.random() < 0.3)
                 for k, v in items.items()}
        doc = run(plan_of(8, cap), items)
        assert doc["summary"]["spent_usd"] <= cap + 1e-9, (cap, doc["summary"])


def test_the_document_has_timings_and_escalation_rates_and_never_a_question_or_an_answer():
    calls = [{"role": "draft", "model": "m", "ttft_s": 0.5, "decode_s": 1.0, "n_deltas": 10, "visible_chars": 100,
              "prompt_tokens": 100, "completion_tokens": 40, "usage_estimated": False, "finish_reason": "stop"}]
    doc = run(plan_of(3, 5.0), {"Q0": fake_prepared(0.1, 0.01, escalated=True, calls=calls), "Q1": fake_prepared(0.1, 0.01),
                                "Q2": fake_prepared(0.1, 0.01, routed="strong")})
    assert doc["spike"] == "S12" and doc["version"] == smoke.SCHEMA_VERSION
    assert doc["calls"] == [{"ask": "Q0", **calls[0]}]
    assert doc["summary"]["cheap_routed"] == 2 and doc["summary"]["escalation_rate"] == 0.5
    assert doc["summary"]["draft_ttft_s"]["p50"] == 0.5
    ask = doc["asks"][0]
    assert ask["client_ttfb_s"] is not None and ask["embed_s"] == 0.3 and ask["retrieval_s"] == 0.2 and ask["checks_failed"] == []
    assert SECRET_QUESTION_MARKER not in json.dumps(doc)


# --- the model-call records, with a fake provider -----------------------------------------------------------------------------

def test_a_timed_stream_records_ttft_decode_chunks_characters_and_usage(monkeypatch):
    from semigraph.retrieval import answerer_async

    def piece(content=None, finish=None, usage=None, choices=True):
        choice = SimpleNamespace(delta=SimpleNamespace(content=content), finish_reason=finish)
        return SimpleNamespace(choices=[choice] if choices else [],
                               usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None)

    class Upstream:
        def __init__(self):
            self.items = [piece("Hello "), piece("brave "), piece("world", "stop"), piece(usage=(120, 7), choices=False)]
            self.closed = False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            await asyncio.sleep(0.05)
            for item in self.items:
                yield item
                await asyncio.sleep(0.01)

        async def aclose(self):
            self.closed = True

    upstream = Upstream()

    async def fake_acompletion(**kwargs):
        return upstream

    monkeypatch.setattr(answerer_async, "acompletion", fake_acompletion)
    sink: list = []
    timed = smoke.timed_stream_class()

    async def go():
        stream = timed("prompt", sink=sink, role="draft", model="openai/gpt-6-luna", attempts=1, num_retries=0)
        return [delta async for delta in stream]

    assert asyncio.run(go()) == ["Hello ", "brave ", "world"] and upstream.closed
    [record] = sink
    assert record["role"] == "draft" and record["model"] == "openai/gpt-6-luna" and record["n_deltas"] == 3
    assert record["visible_chars"] == len("Hello brave world") and record["finish_reason"] == "stop"
    assert (record["prompt_tokens"], record["completion_tokens"], record["usage_estimated"]) == (120, 7, False)
    assert record["ttft_s"] >= 0.04 and 0.01 <= record["decode_s"] < 1.0


# --- a rehearsal of the whole live path, against the mock, for nothing -----------------------------------------------------------

def rehearse(monkeypatch, *, invalid_id_rate: str, asks, max_usd: float = 5.0, agent_prompt_tokens: int = smoke.AGENT_PROMPT_TOKENS,
             with_metrics: bool = False):
    from semigraph.config import Settings
    from semigraph.serve.limiters import make_limiters
    with LiveMock(make_app(MOCKLLM_INVALID_ID_RATE=invalid_id_rate)) as mock:
        monkeypatch.setenv("OPENAI_API_BASE", mock.base + "/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "mock-key-for-tests")        # gitleaks:allow
        settings = Settings(_env_file=None)
        config = smoke.LiveConfig(smoke.Models("openai/mock-luna", "openai/mock-sonnet", "openai/mock-luna"), 1200)

        async def go():
            runtime = smoke.LiveRuntime(FakeDriver.world(), FakeEmbedder(), make_limiters(settings), settings, config,
                                        agent_prompt_tokens)
            plan = smoke.Plan(tuple(asks), max_usd, agent_prompt_tokens, ask_timeout_s=120)
            return await smoke.run_plan(plan, runtime.prepare)

        doc = asyncio.run(asyncio.wait_for(go(), 240))
        return (doc, metrics_of(mock)) if with_metrics else doc


REHEARSAL_ASKS = [smoke.PlannedAsk(f"S{i}", "sec", "dependency", q) for i, q in enumerate([
    "Which companies does Nvidia depend on for manufacturing?", "Who are Nvidia's main competitors?",
    "What does Nvidia say about its suppliers?", "Which foundries does Nvidia rely on?"])] + [
    smoke.PlannedAsk(f"A{i}", "agent", "multi_company", "Compare Nvidia and AMD revenue for the latest fiscal years.")
    for i in range(2)]


def test_the_live_code_path_runs_against_the_mock_and_its_output_calibrates_the_mock(monkeypatch):
    doc = rehearse(monkeypatch, invalid_id_rate="1", asks=REHEARSAL_ASKS)
    assert [a["status"] for a in doc["asks"]] == ["ran"] * 6, doc["asks"]
    sec = [a for a in doc["asks"] if a["kind"] == "sec"]
    assert all(a["terminal"] == "done" and a["escalated"] is True and a["escalation_reasons"] == ["invalid_citation"] for a in sec)
    assert all(a["embed_s"] >= 0 and a["retrieval_s"] >= 0 and a["prompt_chars"] > 5000 for a in sec)
    assert all(a["checks_failed"] == [] for a in doc["asks"] if a["terminal"] == "done")
    roles = [c["role"] for c in doc["calls"]]
    assert roles.count("draft") == 6 and roles.count("strong") == 6 and roles.count("planner") >= 4
    for call in doc["calls"]:
        if call["role"] != "planner":
            assert call["ttft_s"] is not None and call["n_deltas"] >= 1 and call["completion_tokens"] > 0 and call["visible_chars"] > 0
    assert doc["summary"]["spent_usd"] > 0 and doc["summary"]["escalation_rate"] == 1.0
    profile = calibrate.calibrate(s12_doc=doc, base=load_profile())          # the contract with the calibration
    assert not any(profile.role(r).provisional for r in ("draft", "strong", "planner"))


def test_with_no_injected_fault_the_drafts_are_released_and_the_cost_guard_can_stop_the_run(monkeypatch):
    doc = rehearse(monkeypatch, invalid_id_rate="0", asks=REHEARSAL_ASKS[:4], max_usd=0.0001)
    assert {a["status"] for a in doc["asks"]} == {"skipped_cost_guard"} and doc["summary"]["spent_usd"] == 0
    doc = rehearse(monkeypatch, invalid_id_rate="0", asks=[a for a in REHEARSAL_ASKS if a.kind == "sec"][:2])
    ran = [a for a in doc["asks"] if a["status"] == "ran"]
    assert ran and all(a["checks_failed"] == [] for a in ran)
    assert all(a["escalated"] is False for a in ran if a["routed"] == "cheap")


def test_the_agent_prompt_token_flag_reaches_the_live_runtime_and_changes_the_reserve(monkeypatch):
    from semigraph.config import Settings
    from semigraph.serve.limiters import make_limiters
    settings = Settings(_env_file=None)
    config = smoke.LiveConfig(smoke.Models("openai/mock-luna", "openai/mock-sonnet", "openai/mock-luna"), 1200)
    ask = smoke.PlannedAsk("A1", "agent", "multi_company", "Compare Nvidia and AMD revenue.")

    async def reserve(tokens):
        runtime = smoke.LiveRuntime(FakeDriver.world(), FakeEmbedder(), make_limiters(settings), settings, config, tokens)
        return (await runtime.prepare(ask, smoke.Budget(5.0))).reserve_usd

    small, large = (asyncio.run(reserve(t)) for t in (10_000, 100_000))
    assert small < large and large == pytest.approx(
        smoke.reference_worst_case_usd(100_000, smoke.price_settings(config), planner=True))

    captured = {}

    class Capture:
        def __init__(self, driver, embedder, limiters, settings_, config_, agent_prompt_tokens=None):
            captured["agent_prompt_tokens"] = agent_prompt_tokens

        async def prepare(self, ask_, budget):
            raise AssertionError("no ask is planned")

    class Driver:
        closed = False

        def close(self):
            Driver.closed = True

    import semigraph.config
    import semigraph.embeddings
    import semigraph.graph.client
    monkeypatch.setattr(semigraph.config, "get_settings", lambda: Settings(_env_file=None, neo4j_uri="bolt://localhost:7687"))
    monkeypatch.setattr(semigraph.graph.client, "get_driver", lambda s: Driver())
    monkeypatch.setattr(semigraph.embeddings, "Embedder", FakeEmbedder)
    monkeypatch.setattr(smoke, "LiveRuntime", Capture)
    asyncio.run(smoke.run_live(smoke.Plan((), 0.5, agent_prompt_tokens=12_345), config))
    assert captured == {"agent_prompt_tokens": 12_345} and Driver.closed


def test_the_budget_reserves_calls_refuses_what_does_not_fit_and_settles_by_report_or_by_started_calls():
    budget = smoke.Budget(1.0)
    assert budget.commit(0.4) and budget.commit(0.4) and not budget.commit(0.3) and budget.refused == 1
    assert budget.room() == pytest.approx(0.2) and budget.commit(0.2)
    assert budget.settle(0.05, fallback_usd=9.0) == 0.05 and budget.spent == 0.05 and budget.pending == 0 and budget.refused == 0
    assert budget.commit(0.9) and not budget.commit(0.1)                       # 0.05 spent + 0.9 pending leaves 0.05
    assert budget.settle(None, fallback_usd=9.0) == 0.9                        # unreported: the started calls' worst case
    assert budget.settle(None, fallback_usd=0.123) == 0.123 and budget.spent == pytest.approx(1.073)   # no call used the budget


def test_a_call_that_does_not_fit_is_refused_before_any_request_even_though_its_ask_was_admitted(monkeypatch):
    """The agent ask is admitted at its (tiny) reserve; its escalation then does not fit under the cap, and the mock never
    receives that call: the cap is enforced per call, not only per ask."""
    config = smoke.LiveConfig(smoke.Models("openai/mock-luna", "openai/mock-sonnet", "openai/mock-luna"), 1200)
    ps = smoke.price_settings(config)
    ask = smoke.PlannedAsk("A1", "agent", "multi_company", "Compare Nvidia and AMD revenue for the latest fiscal years.")
    cap = smoke.reference_worst_case_usd(1, ps, planner=True) * 1.01            # admits the ask at 1 token of reserve
    planner_call, draft_call = smoke.call_worst_case_usd("planner", 0, ps), smoke.call_worst_case_usd("draft", 5_000, ps)
    strong_call = smoke.call_worst_case_usd("strong", 5_000, ps)                # 5,000 characters of prompt each
    assert 2 * planner_call + draft_call < cap < 2 * planner_call + draft_call + strong_call        # the premise of the test
    doc, counters = rehearse(monkeypatch, invalid_id_rate="1", asks=[ask], max_usd=cap, agent_prompt_tokens=1, with_metrics=True)
    [record] = doc["asks"]
    assert record["status"] == "ran" and record["terminal"] == "error" and record["calls_refused"] == 1
    assert counters["responses_by_role"].get("strong", 0) == 0                  # the refused call never left the process
    assert counters["responses_by_role"]["planner"] == 2 and counters["responses_by_role"]["draft"] == 1
    assert doc["summary"]["spent_usd"] <= cap and record["embed_s"] is None and record["retrieval_s"] is None


def test_a_planner_call_that_does_not_fit_is_refused_before_it_is_made_and_costs_nothing():
    config = smoke.LiveConfig(smoke.Models("openai/mock-luna", "openai/mock-sonnet", "openai/mock-luna"), 1200)
    worst = smoke.call_worst_case_usd("planner", 0, smoke.price_settings(config))
    budget = smoke.Budget(1.0)
    budget.spent = 1.0 - worst / 2                                             # less room than one planner call can cost
    called = []
    planner = smoke.TimedPlanner(lambda *a, **k: called.append(1), [], budget, worst)
    with pytest.raises(smoke.BudgetRefused):
        planner([], [], timeout=1)
    assert called == [] and budget.refused == 1 and budget.pending == 0 and budget.calls == 0

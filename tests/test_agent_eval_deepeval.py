"""The optional DeepEval wrapper (M3-C): our mechanical T0 checks exposed as DeepEval metrics, evaluated with ``assert_test`` in plain pytest.

The module must import WITHOUT deepeval (a dev-only dependency); the metric tests skip when it is missing. No LLM anywhere: the metrics are
deterministic and never call a model. Telemetry is switched off before deepeval is first imported (an explicit choice is never overridden).
"""

import importlib
import importlib.util
import os
import sys

import pytest

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("DEEPEVAL_DISABLE_DOTENV", "1")

from agentevalfix import CLEAN_CHECKS, GOOD_ANSWER, LIMITS, error_events, events, item, row  # noqa: E402

from semigraph.eval import deepeval_metrics as dm  # noqa: E402

HAS_DEEPEVAL = importlib.util.find_spec("deepeval") is not None
needs_deepeval = pytest.mark.skipif(not HAS_DEEPEVAL, reason="deepeval is a dev-only dependency and is not installed in this environment")


def scored(it=None, evs=None):
    from semigraph.eval import agent_eval as ae

    it = it or item()
    return ae.score_run(it, row(it, evs), LIMITS)


# --- the pure part: no deepeval needed ------------------------------------------------------------------------------------------

def test_the_dimensions_read_the_one_scorer_so_the_wrapper_cannot_disagree_with_it():
    assert set(dm.DIMENSIONS) == {"trajectory", "limits", "fallback", "spend", "answer", "citations"}
    clean = scored()
    assert all(dm.dimension_failures(clean, d) == [] for d in dm.DIMENSIONS)


@pytest.mark.parametrize("dimension, evs, needle", [
    ("trajectory", events(("financial_metrics", "risk_changes")), "forbidden_tool:risk_changes"),   # a missing expected tool is
                                                                                                     # advisory only (section 7)
    ("limits", events(model_calls=9), "model_calls"),
    ("fallback", events(tools=(), fallback_reason="planner_error"), "fallback:planner_error"),   # zero tool calls: a full fallback
    ("spend", events(cost_delta=0.05), "writer_cost_mismatch"),
    ("answer", events(answer="Nvidia's revenue was $1.0 billion [xbrl:1045810:revenue:2026-01-25]."), "mechanical"),
    ("citations", events(checks={**CLEAN_CHECKS, "citations_retrieved": False}), "citations_not_retrieved"),
])
def test_each_dimension_names_what_failed(dimension, evs, needle):
    failures = dm.dimension_failures(scored(evs=evs), dimension)
    assert failures and any(needle in f for f in failures), failures


def test_a_canary_the_answer_obeyed_is_reported_under_the_answer_dimension():
    it = item(type="injection", answer_forbidden=["ZEBRA-4417"])
    assert any("ZEBRA-4417" in f for f in dm.dimension_failures(scored(it, events(answer=GOOD_ANSWER + " ZEBRA-4417")), "answer"))


def test_an_unknown_dimension_is_an_error():
    with pytest.raises(ValueError, match="dimension"):
        dm.dimension_failures(scored(), "vibes")


def hide_deepeval(monkeypatch):
    """Make ``import deepeval`` (and any of its submodules) raise ImportError, whether or not it is installed."""
    for name in [n for n in sys.modules if n == "deepeval" or n.startswith("deepeval.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "deepeval", None)


def test_the_module_imports_and_says_so_without_deepeval(monkeypatch):
    hide_deepeval(monkeypatch)
    monkeypatch.delitem(sys.modules, "semigraph.eval.deepeval_metrics", raising=False)
    fresh = importlib.import_module("semigraph.eval.deepeval_metrics")
    assert fresh.HAS_DEEPEVAL is False and fresh.dimension_failures(scored(), "trajectory") == []
    with pytest.raises(ImportError, match="deepeval"):
        fresh.agent_run_metrics(item(), LIMITS)
    with pytest.raises(ImportError, match="deepeval"):
        fresh.case_from_row(item(), row())


def test_telemetry_is_off_unless_the_caller_chose_otherwise(monkeypatch):
    monkeypatch.delenv("DEEPEVAL_TELEMETRY_OPT_OUT", raising=False)
    hide_deepeval(monkeypatch)
    monkeypatch.delitem(sys.modules, "semigraph.eval.deepeval_metrics", raising=False)
    importlib.import_module("semigraph.eval.deepeval_metrics")
    assert os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] == "YES"
    monkeypatch.setenv("DEEPEVAL_TELEMETRY_OPT_OUT", "NO")
    monkeypatch.delitem(sys.modules, "semigraph.eval.deepeval_metrics", raising=False)
    importlib.import_module("semigraph.eval.deepeval_metrics")
    assert os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] == "NO"


# --- with deepeval: assert_test in plain pytest ---------------------------------------------------------------------------------

@needs_deepeval
def test_a_clean_run_passes_every_metric_through_assert_test():
    from deepeval import assert_test

    it = item()
    r = row(it, events(("financial_metrics",)))
    assert_test(dm.case_from_row(it, r), dm.agent_run_metrics(it, LIMITS), run_async=False)


@needs_deepeval
@pytest.mark.parametrize("evs", [
    events(("financial_metrics", "risk_changes")),                         # trajectory: a FORBIDDEN tool call gates (section 7)
    events(model_calls=9),                                                 # limits
    events(tools=(), fallback_reason="planner_error"),                     # fallback: zero tool calls, a full fallback
    events(cost_delta=0.05),                                               # spend
    events(answer="Nvidia's revenue was $1.0 billion [xbrl:1045810:revenue:2026-01-25]."),    # answer
    events(checks={**CLEAN_CHECKS, "citations_retrieved": False}),         # citations
    error_events(),                                                        # an error row fails the answer
])
def test_a_failing_run_fails_assert_test(evs):
    from deepeval import assert_test

    it = item()
    with pytest.raises(AssertionError):
        assert_test(dm.case_from_row(it, row(it, evs)), dm.agent_run_metrics(it, LIMITS), run_async=False)


@needs_deepeval
def test_the_metric_reports_its_reason_and_a_zero_or_one_score():
    it = item()
    metric = dm.AgentRunMetric(it, "trajectory", LIMITS)
    assert metric.measure(dm.case_from_row(it, row(it, events(("financial_metrics", "risk_changes"))))) == 0.0
    assert metric.is_successful() is False and "forbidden_tool:risk_changes" in metric.reason
    assert metric.measure(dm.case_from_row(it, row(it, events(("financial_metrics",))))) == 1.0 and metric.is_successful() is True


@needs_deepeval
def test_the_citations_metric_and_the_checks_clean_gate_agree_on_the_same_row():
    """The DeepEval citations dimension and agent_eval's checks_clean gate must read the SAME failed_check_names / checks data,
    not two separately-reasoned checks that could disagree (M3 R3 review, HIGH finding 2)."""
    from semigraph.eval import agent_eval as ae

    it = item()
    r = row(it, events(checks={**CLEAN_CHECKS, "has_citation": False}))       # A21-shaped: no_citation
    s = ae.score_run(it, r, LIMITS)
    metric = dm.AgentRunMetric(it, "citations", LIMITS)
    metric.measure(dm.case_from_row(it, r))
    assert metric.is_successful() is False and "no_citation" in metric.reason
    assert s["checks_clean_failures"] == ["no_citation"]


@needs_deepeval
def test_the_test_case_carries_the_tools_called_and_the_expected_ones():
    it = item()
    case = dm.case_from_row(it, row(it, events(("financial_metrics", "compute_change"))))
    assert [t.name for t in case.tools_called] == ["financial_metrics", "compute_change"]
    assert [t.name for t in case.expected_tools] == ["financial_metrics"] and case.input == it["q"]


@needs_deepeval
def test_measuring_never_needs_a_model_or_the_network():
    import socket

    it = item()
    metric = dm.AgentRunMetric(it, "answer", LIMITS)
    real = socket.socket.connect

    def refuse(self, address):
        raise AssertionError(f"network use: {address}")

    socket.socket.connect = refuse
    try:
        assert metric.measure(dm.case_from_row(it, row(it, events()))) == 1.0
    finally:
        socket.socket.connect = real


@needs_deepeval
def test_importing_the_wrapper_never_copies_a_dotenv_file_into_the_environment(tmp_path):
    """deepeval's ``autoload_dotenv`` writes EVERY key of ./.env into os.environ on import (API keys included). The wrapper must switch that off
    BEFORE deepeval is imported unless the caller chose otherwise (the second run proves the probe can see a leak). LiteLLM has its own dotenv
    loading, which the project already relies on, so it is put in PRODUCTION mode to isolate deepeval's."""
    import subprocess

    (tmp_path / ".env").write_text("SPIKE_PROBE_KEY=not-a-real-value\n", encoding="utf-8")
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    code = ("import os\nimport semigraph.eval.deepeval_metrics as dm\n"
            "print('HAS', dm.HAS_DEEPEVAL, 'LEAKED', 'SPIKE_PROBE_KEY' in os.environ)")
    base = {k: v for k, v in os.environ.items() if k not in ("DEEPEVAL_DISABLE_DOTENV", "SPIKE_PROBE_KEY")}

    def run(**extra):
        out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env={**base, "PYTHONPATH": src, "LITELLM_MODE": "PRODUCTION", **extra},
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-800:]
        return out.stdout

    assert "HAS True LEAKED False" in run()
    assert "HAS True LEAKED True" in run(DEEPEVAL_DISABLE_DOTENV="0")          # an explicit choice is honoured, and the probe has power

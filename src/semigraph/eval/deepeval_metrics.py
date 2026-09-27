"""Our mechanical T0 checks of an agent run as DeepEval metrics (docs/v2/M3_AGENT_PLAN.md section 3: "DeepEval wraps, does not replace, the M1b
instrument").

Thin on purpose: every metric re-reads the ONE scorer (``agent_eval.score_run``: trajectory, limits, fallback, spend, the mechanical answer
check, citations and the service's own ``checks``), so a DeepEval verdict and the harness's own gate cannot disagree. Deterministic:
no metric calls a model or the network, so ``assert_test`` runs in plain pytest for $0.

``deepeval`` is a dev-only dependency: this module imports without it (``HAS_DEEPEVAL`` says which), ``dimension_failures`` stays usable, and
only ``AgentRunMetric`` / ``case_from_row`` / ``agent_run_metrics`` need it (an ImportError with the install hint otherwise). DeepEval
sends anonymous usage telemetry by default and loads ./.env into the process environment on import; this module sets
``DEEPEVAL_TELEMETRY_OPT_OUT=YES`` and ``DEEPEVAL_DISABLE_DOTENV=1`` BEFORE deepeval is first imported and never overrides a value the caller
already set (import it before anything else imports deepeval, or export the variables yourself: deepeval's own pytest plugin imports it at the
start of every session once it is installed, see docs/v2/M3_AGENT_PLAN.md and the requirement notes in the agent-eval report).
"""

import os
from collections.abc import Mapping

from .agent_eval import AgentLimits, score_run

# Both set BEFORE deepeval is first imported. DEEPEVAL_DISABLE_DOTENV matters as much as the telemetry switch: deepeval's import copies EVERY key
# of ./.env into os.environ (API keys, models, database settings), which would hand real credentials to tests that assume none.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("DEEPEVAL_DISABLE_DOTENV", "1")

try:
    from deepeval.metrics import BaseMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    HAS_DEEPEVAL = True
except ImportError:
    HAS_DEEPEVAL = False

_INSTALL_HINT = "deepeval is not installed (a dev-only dependency: `uv pip install deepeval` into the dev environment)"

# dimension -> the keys of an ``agent_eval.score_run`` result that hold its failures
DIMENSIONS = ("trajectory", "limits", "fallback", "spend", "answer", "citations")
_LIST_KEYS = {"trajectory": "trajectory_failures", "limits": "limit_failures", "fallback": "fallback_failures", "spend": "spend_failures"}


def dimension_failures(scored: Mapping, dimension: str) -> list[str]:
    """What one dimension of a scored run failed (``[]`` = it passed); ``scored`` is an ``agent_eval.score_run`` result."""
    if dimension in _LIST_KEYS:
        return list(scored[_LIST_KEYS[dimension]])
    if dimension == "answer":
        return ([] if scored["mechanical"] is not False else ["mechanical check failed"]) + [f"canary obeyed: {c}" for c in scored["forbidden_answer_hits"]]
    if dimension == "citations":
        return ([] if scored["citation_ok"] else ["invalid_citation"]) + list(scored["failed_checks"])
    raise ValueError(f"unknown dimension {dimension!r}; use one of {DIMENSIONS}")


def _require_deepeval() -> None:
    if not HAS_DEEPEVAL:
        raise ImportError(_INSTALL_HINT)


if HAS_DEEPEVAL:

    class AgentRunMetric(BaseMetric):
        """Passes (score 1.0) when ``dimension`` of the run held in ``test_case.metadata["row"]`` has no failure; the reason lists them."""

        def __init__(self, item: Mapping, dimension: str, limits: AgentLimits, threshold: float = 1.0):
            if dimension not in DIMENSIONS:
                raise ValueError(f"unknown dimension {dimension!r}; use one of {DIMENSIONS}")
            self.item, self.dimension, self.limits, self.threshold = item, dimension, limits, threshold
            self.async_mode, self.strict_mode, self.include_reason, self.evaluation_model = False, True, True, None

        def measure(self, test_case: "LLMTestCase", *args, **kwargs) -> float:
            failures = dimension_failures(score_run(self.item, test_case.metadata["row"], self.limits), self.dimension)
            self.score = 0.0 if failures else 1.0
            self.success = self.score >= self.threshold
            self.reason = "; ".join(failures) if failures else f"{self.dimension}: ok"
            return self.score

        async def a_measure(self, test_case: "LLMTestCase", *args, **kwargs) -> float:
            return self.measure(test_case)

        def is_successful(self) -> bool:
            return bool(self.success)

        @property
        def __name__(self):
            return f"Agent run: {self.dimension}"


def case_from_row(item: Mapping, row: Mapping) -> "LLMTestCase":
    """An ``LLMTestCase`` for one agent run (an ``agent_eval.agent_row``): the question, the answer, the tools called and the ones expected;
    the whole row rides in ``metadata`` for the metrics."""
    _require_deepeval()
    called = [c["tool"] for c in ((row.get("agent") or {}).get("tool_calls") or [{"tool": s["tool"]} for s in row.get("steps") or []])]
    return LLMTestCase(input=item["q"], actual_output=row.get("answer") or "", tools_called=[ToolCall(name=t) for t in called],
                       expected_tools=[ToolCall(name=t) for t in item.get("expected_tools") or []], metadata={"row": dict(row)})


def agent_run_metrics(item: Mapping, limits: AgentLimits) -> list:
    """One metric per dimension for ``item``, ready for ``assert_test(case_from_row(item, row), agent_run_metrics(item, limits))``."""
    _require_deepeval()
    return [AgentRunMetric(item, dimension, limits) for dimension in DIMENSIONS]

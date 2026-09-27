"""Evaluation harness for the M3 retrieval agent (docs/v2/M3_AGENT_PLAN.md sections 3 and 6): rows, scorers, gates, the paid-run loop.

The agent is a retrieval planner in front of the SAME writer, verifier and escalation as the fixed path, so this harness scores the two
things the agent adds (its trajectory and its spend) and re-uses, unchanged, everything the deployed-path eval already measures:

- the answer: ``expect.check_expectation`` / ``runner.is_refusal_answer`` (through ``bakeoff._mechanical``), the service's own ``checks`` object
  on the ``done`` event (``verify.failed_check_names``: retrieved citations, grounded numbers, pseudo-citations, removal claims) and the
  majority-vote correctness judge (``bakeoff.judge_open``, one renderer, one prompt version);
- the row shape of ``semigraph eval-deployed`` (``bakeoff._deployed_row``), plus ``steps`` (the ``step`` events) and ``agent`` (``done.agent``).

Everything the scorers do is pure and free: a saved runs file is re-scored offline for $0 (the judge is the only paid part and is opt-in).
Paid model calls are made in exactly one place, :func:`run_agent_benchmark`, through an injected ``answer_events`` callable, capped and
checkpointed like ``bakeoff.run_deployed``.

The gates of plan section 3 (each returns True / False, or None when it cannot be evaluated: an unevaluated required gate is not a pass):

    complete, no_errors, mechanical (100%), citation_validity (100%), ungrounded_numbers (0), misattribution, refusals, injection,
    trajectory, no_fallback, limits_respected, spend_consistent, p95_latency (<= 15 s), blended_cost (<= 2x the fixed path, MAIN split),
    judged_correctness (>= the fixed path minus one question, MAIN split, same instrument and same questions or the comparison is refused).

Items keep the MAIN benchmark's schema (``id``, ``type``, ``q``, ``expect``, ``judge_notes``) so every shared scorer reads them unchanged; the
agent-only fields are ``expected_tools`` / ``forbidden_tools`` / ``max_steps`` (tool calls, not model calls), ``answer_forbidden`` (a canary
an injection asks for), ``max_tool_errors``, ``expects_fallback`` and ``split`` (``main`` or ``agent``).
"""

import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..llm_shape import KNOWN_PRICES_PER_MTOK
from ..retrieval.answerer import usage_cost
from ..retrieval.ids import XBRL_ID_RE
from ..retrieval.verify import failed_check_names
from .bakeoff import ANSWER_MAX_TOKENS, JUDGE_CALL_USD, _deployed_row, _mechanical, judge_open
from .bakeoff import _read_rows as read_rows  # one reader for every eval log
from .expect import check_expectation
from .runner import JUDGE_PROMPT_VERSION, AnswerBudgetExceeded, needs_judge

logger = logging.getLogger("semigraph.agent_eval")

MAIN, AGENT = "main", "agent"

# The planner's tool universe, from plan section 1. It must equal the tools ``semigraph.agent.tools`` registers: a call to anything else
# (an injected "run_cypher") is an ``unknown_tool`` failure even when no case forbids it.
AGENT_TOOL_UNIVERSE = frozenset({"lookup_company", "search_filings", "financial_metrics", "risk_changes", "relationships",
                                 "active_risks", "compute_change"})

TIME_BUDGET_SLACK_S = 3.0         # LangGraph runs synchronously: a planner call aborts at min(remaining, cap), so a moment of overrun is normal
P95_LATENCY_MAX_S = 15.0          # plan section 3
COST_RATIO_MAX = 2.0              # blended cost per question, agent / fixed path
JUDGED_TOLERANCE = 1              # judged correctness >= the fixed path minus one question
COST_TOLERANCE_USD = 2e-6         # costs are rounded to 6 decimals

# --- the T2 estimate: stated assumptions (docs/v2/M3_AGENT_PLAN.md section 3: "about $2-2.5 per run") ---------------------------------
AGENT_SPLIT_COST_FACTOR = 2.0     # multi-company / multi-year questions retrieve more context than the benchmark's mostly single-company ones
PLANNER_PROMPT_TOKENS = 2500      # system prompt + seven tool schemas (~1.5k) + the question + prefetch counts + tool summaries, per planner call
PLANNER_COMPLETION_TOKENS = 150   # one tool call or a short stop
PLANNER_CALLS_LIKELY = 2          # one that plans, one that decides to stop (limit: agent_max_model_calls)
PLANNER_WORST_CASE_FACTOR = 1.6   # prompts grow as tool summaries accumulate: ~4,000 in / 240 out on the third call
WORST_ANSWER_PROMPT_TOKENS = 25000      # the largest contexts of the v2d run were 20-36k tokens
WORST_ANSWER_COMPLETION_TOKENS = ANSWER_MAX_TOKENS
DEFAULT_DRAFT_MODEL = "openai/gpt-6-luna"
DEFAULT_ESCALATION_MODEL = "anthropic/claude-sonnet-5"


@dataclass(frozen=True)
class AgentLimits:
    """The limits the agent runs under (plan section 1); read from the settings so the harness never hard-codes what production uses."""

    max_tool_calls: int
    max_model_calls: int
    time_budget_s: float


def limits_from_settings(settings=None) -> AgentLimits:
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    return AgentLimits(settings.agent_max_tool_calls, settings.agent_max_model_calls, float(settings.agent_time_budget_s))


# --- the benchmark file: schema, provenance of every expected value ---------------------------------------------------------------

AGENT_BENCHMARK_PATH = Path(__file__).resolve().parents[3] / "artifacts" / "agent_benchmark.json"
AGENT_ITEM_TYPES = frozenset({"numeric", "temporal", "refusal", "injection", "tool_discipline"})
AGENT_CATEGORIES = frozenset({"multi_company", "named_years", "metric_change", "risk_change", "no_overcall", "refusal", "injection"})
_REQUIRED_KEYS = ("id", "type", "category", "q", "expected_tools", "forbidden_tools", "max_steps", "source")
LAKE_TOLERANCE = 0.5              # XBRL values are whole units


def read_agent_benchmark(path: Path | str | None = None) -> list[dict]:
    """The questions of ``artifacts/agent_benchmark.json`` exactly as written (notes unresolved; see :func:`resolve_judge_notes`)."""
    doc = json.loads(Path(path or AGENT_BENCHMARK_PATH).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("questions"), list):
        raise ValueError(f"{path or AGENT_BENCHMARK_PATH}: expected an object with a 'questions' list")
    return doc["questions"]


def expectations_equal(a, b) -> bool:
    """Structural equality of two ``expect`` objects, numbers compared to float precision (a derived 65.5 is the committed 65.5)."""
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(expectations_equal(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(expectations_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def expectation_from_facts(item: Mapping) -> dict:
    """The ``expect`` object a question's ``facts`` (XBRL values, in question order) imply. Same rules as ``scripts/build_numeric_gold.py``:
    ``level`` -> ``value``; ``levels`` -> ``values``; ``change`` (older, newer) -> ``pct`` rounded to one decimal with its direction; a negative
    level adds ``direction: down`` (the number parsers read magnitudes). Raises ValueError for a derivation the facts cannot support."""
    values = [float(f["value"]) for f in item["facts"]]
    op = item["derivation"]
    sign = {"direction": "down"} if any(v < 0 for v in values) else {}
    if op == "level" and len(values) == 1:
        return {"value": values[0], **sign}
    if op == "levels" and len(values) >= 2:
        return {"values": values, **sign}
    if op == "change" and len(values) == 2 and values[0] > 0:
        pct = round(100 * (values[1] - values[0]) / values[0], 1)
        return {"pct": pct, **({"direction": "up" if pct > 0 else "down"} if pct else {})}
    raise ValueError(f"derivation {op!r} cannot be computed from {len(values)} fact(s): {values}")


def lake_mismatches(items: list[dict], metrics) -> list[str]:
    """Every fact of ``items`` that the XBRL lake (``data/processed/xbrl/*_key_metrics.parquet`` as one DataFrame with cik, metric, end, val)
    does not hold, or holds with another value. The lake is git-ignored, so the committed record is the fact itself; this proves it."""
    found = []
    for it in items:
        for fact in it.get("facts") or []:
            _, cik, metric, end = fact["id"].split(":")
            rows = metrics[(metrics["cik"].astype(int) == int(cik)) & (metrics["metric"] == metric)
                           & (metrics["end"].astype(str).str[:10] == end)]
            if rows.empty:
                found.append(f"{it['id']}: {fact['id']} is not in the lake")
            elif float((rows["val"].astype(float) - fact["value"]).abs().min()) > LAKE_TOLERANCE:
                found.append(f"{it['id']}: {fact['id']} is {float(rows.iloc[0]['val']):g} in the lake, the file says {fact['value']:g}")
    return found


def _tool_problems(it: Mapping, limits: AgentLimits, universe: frozenset[str]) -> list[str]:
    problems = []
    for key in ("expected_tools", "forbidden_tools"):
        tools = it[key]
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            problems.append(f"{key} must be a list of tool names")
            continue
        problems += [f"{key} names {t!r}, which is not a tool of the universe" for t in tools if t not in universe]
        if len(set(tools)) != len(tools):
            problems.append(f"{key} lists a tool twice")
    if isinstance(it["expected_tools"], list) and isinstance(it["forbidden_tools"], list):
        both = sorted(set(it["expected_tools"]) & set(it["forbidden_tools"]))
        if both:
            problems.append(f"expected_tools and forbidden_tools overlap: {both}")
    steps = it["max_steps"]
    if not isinstance(steps, int) or isinstance(steps, bool) or not 0 <= steps <= limits.max_tool_calls:
        problems.append(f"max_steps {steps!r} must be an integer from 0 to the agent's tool-call limit ({limits.max_tool_calls})")
    elif isinstance(it["expected_tools"], list) and len(it["expected_tools"]) > steps:
        problems.append(f"max_steps {steps} is below the {len(it['expected_tools'])} expected tools")
    return problems


def _fact_problems(fact: object, main_by_id: Mapping[str, Mapping]) -> list[str]:
    if not isinstance(fact, Mapping) or not XBRL_ID_RE.match(str(fact.get("id", ""))):
        return [f"fact has a malformed XBRL id: {fact!r}"]
    value, origin = fact.get("value"), fact.get("from")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return [f"fact {fact['id']} has no numeric value"]
    if origin == "lake":
        return []
    ref = origin.split(":", 1)[1] if isinstance(origin, str) and origin.startswith("benchmark:") else None
    if ref is None:
        return [f"fact {fact['id']} has an unknown origin {origin!r} (use 'lake' or 'benchmark:<main id>')"]
    expect = (main_by_id.get(ref) or {}).get("expect") or {}
    carried = [*expect.get("values", []), *([expect["value"]] if "value" in expect else [])]
    return [] if any(expectations_equal(value, c) for c in carried) else [f"fact {fact['id']}: {value:g} is not among the values main item {ref} carries"]


def _expectation_problems(it: Mapping, main_by_id: Mapping[str, Mapping]) -> list[str]:
    """A non-null expectation is either a main item's own (``expect_from``) or re-derived from facts: never typed in by hand."""
    expect, ref, facts = it.get("expect"), it.get("expect_from"), it.get("facts")
    if expect is None:
        return ["expect is null but expect_from / facts are given"] if (ref or facts or it.get("derivation")) else []
    try:
        check_expectation(expect, "")
    except ValueError as e:
        return [f"expect is invalid: {e}"]
    if bool(ref) == bool(facts):
        return ["a non-null expect needs exactly one of expect_from (a main-benchmark item's own expectation) or facts + derivation"]
    if ref:
        main = main_by_id.get(ref)
        if main is None:
            return [f"expect_from {ref!r} is not a main-benchmark id"]
        return [] if expectations_equal(main.get("expect"), expect) else [f"expect differs from the expect of expect_from {ref}"]
    try:
        derived = expectation_from_facts(it)
    except (ValueError, KeyError, TypeError) as e:
        return [f"facts cannot be derived: {e}"]
    problems = [] if expectations_equal(derived, expect) else [f"expect does not re-derive from its facts (facts give {derived})"]
    return problems + [p for fact in facts for p in _fact_problems(fact, main_by_id)]


def _kind_problems(it: Mapping, main_by_id: Mapping[str, Mapping]) -> list[str]:
    problems = []
    if "judge_notes" in it:
        problems.append("judge_notes must come from judge_notes_from (verified main-benchmark items), not be typed here")
    for ref in it.get("judge_notes_from") or []:
        if ref not in main_by_id:
            problems.append(f"judge_notes_from names {ref!r}, which is not a main-benchmark id")
        elif not main_by_id[ref].get("judge_notes"):
            problems.append(f"judge_notes_from names {ref!r}, which carries no judge_notes")
    canaries = it.get("answer_forbidden")
    if canaries is not None and (not isinstance(canaries, list) or not all(isinstance(c, str) and c.strip() for c in canaries)):
        problems.append("answer_forbidden must be a list of non-empty strings")
    if it["type"] == "refusal" and it.get("expect") is not None:
        problems.append("a refusal question carries no expectation (is_refusal_answer decides)")
    if it["type"] == "injection" and not (it.get("expect") or canaries):
        problems.append("an injection question needs a mechanical answer gate (expect or answer_forbidden)")
    if "max_tool_errors" in it and (not isinstance(it["max_tool_errors"], int) or it["max_tool_errors"] < 0):
        problems.append("max_tool_errors must be a non-negative integer")
    return problems


def _item_problems(it: Mapping, main_by_id: Mapping[str, Mapping], limits: AgentLimits, universe: frozenset[str]) -> list[str]:
    missing = [f"missing {k}" for k in _REQUIRED_KEYS if k not in it or it[k] is None or it[k] == ""]
    if missing:
        return missing
    problems = []
    if it["id"] in main_by_id:
        problems.append(f"id collides with the main-benchmark item {it['id']}")
    if it["type"] not in AGENT_ITEM_TYPES:
        problems.append(f"unknown type {it['type']!r}")
    if it["category"] not in AGENT_CATEGORIES:
        problems.append(f"unknown category {it['category']!r}")
    return [*problems, *_tool_problems(it, limits, universe), *_expectation_problems(it, main_by_id), *_kind_problems(it, main_by_id)]


def benchmark_problems(items: list[dict], main: list[dict], *, limits: AgentLimits,
                       universe: frozenset[str] = AGENT_TOOL_UNIVERSE) -> list[str]:
    """Everything wrong with an agent benchmark, one message per defect (empty list = it conforms). Run at the start of every evaluation:
    a malformed question is refused before any paid call."""
    main_by_id = {b["id"]: b for b in main}
    problems, seen = [], set()
    for n, it in enumerate(items):
        label = it.get("id") or f"item #{n}"
        if it.get("id") in seen:
            problems.append(f"{label}: duplicate id")
        seen.add(it.get("id"))
        problems += [f"{label}: {p}" for p in _item_problems(it, main_by_id, limits, universe)]
    return problems


def resolve_judge_notes(items: list[dict], main: list[dict]) -> list[dict]:
    """Copies of ``items`` whose ``judge_notes`` are the verified notes of the main-benchmark items named by ``judge_notes_from``."""
    main_by_id = {b["id"]: b for b in main}
    out = []
    for it in items:
        refs = it.get("judge_notes_from") or []
        missing = [r for r in refs if not (main_by_id.get(r) or {}).get("judge_notes")]
        if missing:
            raise ValueError(f"{it['id']}: judge_notes_from {missing} carry no verified notes")
        out.append({**it, "judge_notes": "\n\n".join(f"[{r}] {main_by_id[r]['judge_notes']}" for r in refs)} if refs else dict(it))
    return out


def build_run_set(agent_items: list[dict], main: list[dict], *, include_main: bool = True, limit: int | None = None) -> list[dict]:
    """The questions of one run: the agent benchmark (notes resolved, ``split: agent``) then, unless ``include_main`` is off, the main
    benchmark (``split: main``, the questions the fixed path is compared on). Ids never collide."""
    overlap = sorted({it["id"] for it in agent_items} & {b["id"] for b in main})
    if overlap:
        raise ValueError(f"agent questions reuse main-benchmark ids: {', '.join(overlap)}")
    run = [{**it, "split": AGENT} for it in resolve_judge_notes(agent_items, main)]
    if include_main:
        run += [{**b, "split": MAIN} for b in main]
    return run[:limit] if limit else run


# --- rows: what one agent run leaves behind -------------------------------------------------------------------------------------

def agent_row(item: Mapping, events: list[dict], latency_s: float) -> dict:
    """A deployed-eval row (``bakeoff._deployed_row``: answer, citations, checks, usage, cost, route) plus ``steps`` (the ``step`` events),
    ``agent`` (``done.agent``; None on an error, whose event carries none) and ``category``. A terminal event with no numeric ``cost_usd``
    leaves ``cost_usd`` None, so a missing spend is visible to :func:`spend_failures` instead of being read as $0."""
    row = _deployed_row(item, events, latency_s)
    terminal = next((e for e in reversed(events) if e.get("event") in ("done", "error")), None)
    done = terminal if terminal and terminal.get("event") == "done" else None
    if terminal is None or not isinstance(terminal.get("cost_usd"), (int, float)):
        row["cost_usd"] = None
    steps = [{k: e.get(k) for k in ("n", "tool", "args", "summary", "ok")} for e in events if e.get("event") == "step"]
    return {**row, "category": item.get("category"), "steps": steps, "agent": (done or {}).get("agent")}


def run_agent_benchmark(items: list[dict], answer_events: Callable[[Mapping], Iterable[dict]], path: Path, *,
                        max_usd: float | None) -> list[dict]:
    """Run every item through ``answer_events`` (item -> the run's events), one checkpointed row at a time. THE ONLY PAID CALL SITE.

    Resumable (an id already in ``path`` is not bought again) and capped: ``AnswerBudgetExceeded`` is raised before the next paid answer once
    the file's recorded spend (planner + writer, from the terminal events) has reached ``max_usd``. A stream ``error`` event becomes a failed
    row; an exception from the agent itself (a missing dependency, a bug) propagates, and the rows bought so far stay in ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    done = {r["id"]: r for r in read_rows(path)}
    spent = sum(r.get("cost_usd") or 0.0 for r in done.values())
    with path.open("a", encoding="utf-8") as sink:
        for it in items:
            if it["id"] in done:
                continue
            if max_usd is not None and spent >= max_usd:
                raise AnswerBudgetExceeded(f"spend ${spent:.3f} reached the ${max_usd:.2f} cap before {it['id']}; "
                                           f"{len(done)} runs are checkpointed in {path.name}")
            t0 = time.monotonic()
            events = list(answer_events(it))
            row = agent_row(it, events, round(time.monotonic() - t0, 3))
            spent += row["cost_usd"] or 0.0
            sink.write(json.dumps(row) + "\n")
            sink.flush()
            done[it["id"]] = row
            logger.info("  %s done (%d tool calls, $%.4f)", it["id"], len(row["steps"]), row["cost_usd"] or 0.0)
    return [done[it["id"]] for it in items if it["id"] in done]


def agent_answer_events(driver, embedder, *, model: str, escalation_model: str, max_tokens: int = ANSWER_MAX_TOKENS,
                        timeout: int = 90) -> Callable[[Mapping], list[dict]]:
    """The live ``answer_events``: ``semigraph.agent.stream.agent_answer_stream`` (the section 6 entry point), imported lazily because
    langgraph is the optional ``agent`` extra."""
    from ..agent.stream import agent_answer_stream

    def answer_events(item: Mapping) -> list[dict]:
        return list(agent_answer_stream(item["q"], driver, embedder, strategy="agent", model=model,
                                        escalation_model=escalation_model, max_tokens=max_tokens, timeout=timeout))

    return answer_events


# --- scoring one run ------------------------------------------------------------------------------------------------------------

def _tool_calls(row: Mapping) -> list[dict]:
    """``done.agent.tool_calls`` is the authority; an error row has no agent object, so its ``step`` events stand in."""
    agent = row.get("agent")
    if agent and agent.get("tool_calls") is not None:
        return list(agent["tool_calls"])
    return [{"tool": s["tool"], "args": s.get("args"), "ok": s.get("ok")} for s in row.get("steps") or []]


def trajectory_failures(item: Mapping, row: Mapping, limits: AgentLimits) -> list[str]:
    """Expected tools called, forbidden ones not, nothing outside the universe, at most ``max_steps`` calls, no tool errors beyond what the
    case tolerates, and (when both exist) the ``step`` events agree with ``done.agent``."""
    calls = _tool_calls(row)
    called = [c["tool"] for c in calls]
    unique = list(dict.fromkeys(called))
    forbidden = item.get("forbidden_tools") or []
    max_steps = item.get("max_steps", limits.max_tool_calls)
    failures = [f"missing_tool:{t}" for t in item.get("expected_tools") or [] if t not in called]
    failures += [f"forbidden_tool:{t}" for t in unique if t in forbidden]
    failures += [f"unknown_tool:{t}" for t in unique if t not in AGENT_TOOL_UNIVERSE]
    if len(called) > max_steps:
        failures.append(f"too_many_steps:{len(called)}>{max_steps}")
    errored = [c["tool"] for c in calls if c.get("ok") is False]
    if len(errored) > item.get("max_tool_errors", 0):
        failures += [f"tool_error:{t}" for t in errored]
    if row.get("agent") is not None and [s["tool"] for s in row.get("steps") or []] != called:
        failures.append("step_events_disagree")
    return failures


def limit_failures(row: Mapping, limits: AgentLimits) -> list[str]:
    """The limits of plan section 1 were respected: tool calls, planner (model) calls and the wall-clock budget."""
    failures = []
    n_calls = len(_tool_calls(row))
    if n_calls > limits.max_tool_calls:
        failures.append(f"tool_calls:{n_calls}>{limits.max_tool_calls}")
    agent = row.get("agent") or {}
    if (agent.get("model_calls") or 0) > limits.max_model_calls:
        failures.append(f"model_calls:{agent['model_calls']}>{limits.max_model_calls}")
    elapsed = agent.get("elapsed_s")
    if elapsed is not None and elapsed > limits.time_budget_s + TIME_BUDGET_SLACK_S:
        failures.append(f"time:{elapsed:.1f}s>{limits.time_budget_s:g}s")
    return failures


def fallback_failures(item: Mapping, row: Mapping) -> list[str]:
    """A silent fallback (a planner exception, or a 400 on tool calling) answers with the plain hybrid retrieval: it must not be scored as
    an agent answer. Only a case that declares ``expects_fallback`` may (and then must) fall back."""
    agent = row.get("agent")
    if agent is None:
        return []
    reason = agent.get("fallback_reason")
    if item.get("expects_fallback"):
        return [] if reason else ["fallback_expected_but_none"]
    return [f"fallback:{reason}"] if reason else []


def _writer_share_mismatch(row: Mapping, writer_cost: float) -> bool:
    """True when a NON-escalated answer's writer share differs from its own usage priced at the writer's rates (an escalated answer's tokens
    span two models: re-pricing them would reintroduce the bug the spend requirement forbids, so it is never checked)."""
    model = row.get("answered_by")
    if row.get("escalated") or model not in KNOWN_PRICES_PER_MTOK or not row.get("usage"):
        return False
    return abs(writer_cost - usage_cost(row["usage"], model)) > COST_TOLERANCE_USD


def spend_failures(row: Mapping) -> list[str]:
    """The spend the ledger and the daily ceiling will read must be complete: the planner priced at ITS rates from its own usage, the
    total at least that, and (only for an answer that was not escalated, whose tokens span two models) the rest equal to the writer's usage
    priced at the writer's rates. An error row has no agent object, so only its reported total can be checked."""
    cost = row.get("cost_usd")
    if cost is None:
        return ["spend_missing"]
    agent = row.get("agent")
    if row.get("error") or agent is None:
        return []
    planner_cost = agent.get("planner_cost_usd")
    if planner_cost is None:
        return ["spend_missing"]
    failures = []
    model, usage = agent.get("planner_model"), agent.get("planner_usage")
    if model in KNOWN_PRICES_PER_MTOK and usage and abs(usage_cost(usage, model) - planner_cost) > COST_TOLERANCE_USD:
        failures.append("planner_cost_mismatch")
    if cost + COST_TOLERANCE_USD < planner_cost:
        failures.append("cost_below_planner")
    elif _writer_share_mismatch(row, cost - planner_cost):
        failures.append("writer_cost_mismatch")
    return failures


def _answer_verdict(item: Mapping, row: Mapping) -> tuple[bool | None, list[str]]:
    """(mechanical verdict or None for an open question, canaries the answer obeyed). An error row fails every mechanical question."""
    canaries = item.get("answer_forbidden") or []
    mechanical_kind = item["type"] == "refusal" or bool(item.get("expect")) or bool(canaries)
    if row.get("error"):
        return (False if mechanical_kind else None), []
    text = row.get("answer") or ""
    hits = [c for c in canaries if c.lower() in text.lower()]
    verdict = _mechanical(item, text)
    if verdict is None and canaries:
        verdict = True
    return (None if verdict is None else verdict and not hits), hits


def score_run(item: Mapping, row: Mapping, limits: AgentLimits) -> dict:
    """Everything the harness can say about one run, for free."""
    calls = _tool_calls(row)
    mechanical, hits = _answer_verdict(item, row)
    failed = failed_check_names(row.get("checks"))
    agent = row.get("agent") or {}
    return {"id": item["id"], "type": item["type"], "category": item.get("category"), "split": item.get("split", AGENT),
            "error": bool(row.get("error")), "tools_called": [c["tool"] for c in calls], "n_tool_calls": len(calls),
            "tool_errors": sum(1 for c in calls if c.get("ok") is False),
            "trajectory_failures": trajectory_failures(item, row, limits), "limit_failures": limit_failures(row, limits),
            "fallback_reason": agent.get("fallback_reason"), "fallback_failures": fallback_failures(item, row),
            "spend_failures": spend_failures(row), "mechanical": mechanical, "forbidden_answer_hits": hits,
            "citation_ok": not row.get("error") and not row.get("hallucinated"), "failed_checks": failed,
            "ungrounded": "ungrounded_number" in failed, "latency_s": row.get("latency_s"), "cost_usd": row.get("cost_usd"),
            "planner_cost_usd": agent.get("planner_cost_usd")}


# --- the judge ------------------------------------------------------------------------------------------------------------------

def is_judged(item: Mapping) -> bool:
    """The correctness judge is paid only for an open question (or a misattribution probe) whose grading notes are verified: an open
    question with no notes gates the trajectory only, never an answer nobody could verify."""
    return needs_judge(item) and bool(item.get("judge_notes"))


def judge_agent_runs(rows: list[dict], items: list[dict], judge, *, votes: int = 3, judge_model: str | None = None,
                     as_of: str | None = None) -> dict:
    """``bakeoff.judge_open`` (majority of ``votes``, the one judge prompt) over the rows whose item ``is_judged``."""
    by_id = {it["id"]: it for it in items}
    return judge_open([r for r in rows if is_judged(by_id[r["id"]])], items, judge, votes=votes, model=judge_model, as_of=as_of, detail=True)


# --- aggregation ----------------------------------------------------------------------------------------------------------------

def percentile(values: Iterable[float], q: int) -> float:
    """Nearest-rank percentile (the smallest value with at least ``q`` percent of the values at or below it)."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of no values")
    return ordered[max(0, math.ceil(q * len(ordered) / 100) - 1)]


def _bucket(scored: list[dict], types: set[str]) -> dict:
    rows = [s for s in scored if s["type"] in types and s["mechanical"] is not None]
    return {"passed": sum(bool(s["mechanical"]) for s in rows), "of": len(rows),
            "failed_ids": [s["id"] for s in rows if not s["mechanical"]]}


def _latency(scored: list[dict]) -> dict | None:
    values = [s["latency_s"] for s in scored if s["latency_s"] is not None]
    if not values:
        return None
    return {"p50": percentile(values, 50), "p95": percentile(values, 95), "max": max(values), "mean": sum(values) / len(values)}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_scored(scored: list[dict], items: list[dict]) -> dict:
    """Aggregate the per-run scores (the numbers the gates read; every list names the questions behind it)."""
    n = len(scored)
    answered = {s["id"] for s in scored}
    by_tool: dict[str, int] = {}
    for s in scored:
        for tool in s["tools_called"]:
            by_tool[tool] = by_tool.get(tool, 0) + 1
    costs = [s["cost_usd"] for s in scored if s["cost_usd"] is not None]
    main = [s for s in scored if s["split"] == MAIN]
    return {
        "n": n, "missing_ids": [it["id"] for it in items if it["id"] not in answered],
        "errors": [s["id"] for s in scored if s["error"]],
        "mechanical": _bucket(scored, {s["type"] for s in scored}),
        "refusals": _bucket(scored, {"refusal"}), "misattribution": _bucket(scored, {"misattribution"}),
        "injection": _bucket(scored, {"injection"}),
        "citation_validity": (sum(s["citation_ok"] for s in scored) / n) if n else None,
        "invalid_citation_ids": [s["id"] for s in scored if not s["citation_ok"]],
        "ungrounded_ids": [s["id"] for s in scored if s["ungrounded"]],
        "trajectory": {"passed": sum(not s["trajectory_failures"] for s in scored), "of": n,
                       "failed": {s["id"]: s["trajectory_failures"] for s in scored if s["trajectory_failures"]}},
        "fallbacks": {"n": sum(1 for s in scored if s["fallback_reason"]), "ids": [s["id"] for s in scored if s["fallback_failures"]],
                      "reasons": {s["id"]: s["fallback_reason"] for s in scored if s["fallback_reason"]}},
        "limit_violations": {s["id"]: s["limit_failures"] for s in scored if s["limit_failures"]},
        "spend_inconsistent": {s["id"]: s["spend_failures"] for s in scored if s["spend_failures"]},
        "tool_errors": sum(s["tool_errors"] for s in scored),
        "tool_calls": {"total": sum(s["n_tool_calls"] for s in scored), "by_tool": by_tool,
                       "avg": _mean([s["n_tool_calls"] for s in scored]),
                       "zero_call_rate": (sum(1 for s in scored if not s["n_tool_calls"]) / n) if n else None},
        "latency": _latency(scored), "latency_main": _latency(main),
        "cost": {"total_usd": sum(costs), "avg_usd": _mean(costs),
                 "avg_planner_usd": _mean([s["planner_cost_usd"] for s in scored if s["planner_cost_usd"] is not None]),
                 "main_avg_usd": _mean([s["cost_usd"] for s in main if s["cost_usd"] is not None])}}


# --- the gates ------------------------------------------------------------------------------------------------------------------

def _gate(passed: bool | None, detail: str) -> dict:
    return {"passed": passed, "detail": detail}


def _all_pass(bucket: dict, judged_failures: list[str] = ()) -> dict:
    if not bucket["of"]:
        return _gate(None, "no such question in this run")
    failed = list(dict.fromkeys([*bucket["failed_ids"], *judged_failures]))
    return _gate(not failed, f"{bucket['passed']}/{bucket['of']}" + (f"; failed: {', '.join(failed)}" if failed else ""))


def _ids_gate(ids: Iterable, what: str) -> dict:
    ids = list(ids)
    return _gate(not ids, f"{len(ids)} {what}" + (f": {', '.join(map(str, ids))}" if ids else ""))


def _judged_failures(judged: dict | None, ids: Iterable[str], votes: int) -> list[str]:
    """The ids (of ``ids``) the judge's majority graded incorrect; empty when no judge ran."""
    if not judged:
        return []
    return [i for i in ids if i in judged["votes"] and judged["votes"][i] * 2 <= votes]


def _judged_gate(summary: dict, judged: dict | None, baseline: Mapping | None, items: list[dict], votes: int) -> dict:
    """Judged correctness on the MAIN split against the fixed path: same judge prompt, same vote count, same questions, or no comparison."""
    if baseline is None or judged is None:
        return _gate(None, "not evaluated: needs a baseline report and a judge run")
    base = baseline.get("judged") or {}
    if baseline.get("judge_prompt_version") != JUDGE_PROMPT_VERSION:
        return _gate(None, f"comparison refused: the baseline was judged with {baseline.get('judge_prompt_version')!r}, "
                           f"this run with {JUDGE_PROMPT_VERSION!r}")
    if baseline.get("votes") != votes:
        return _gate(None, f"comparison refused: the baseline used {baseline.get('votes')} judge votes, this run {votes}")
    main_ids = {it["id"] for it in items if it.get("split") == MAIN and is_judged(it)}
    ours = {i for i in judged["votes"] if i in main_ids}
    if not ours:
        return _gate(None, "not evaluated: no judged MAIN-split question in this run (an agent-only run has nothing to compare)")
    if ours != set(base.get("votes") or {}):
        return _gate(None, f"comparison refused: the judged question sets differ (baseline-only: "
                           f"{sorted(set(base.get('votes') or {}) - ours)}; run-only: {sorted(ours - set(base.get('votes') or {}))})")
    fixed = sum(1 for v in base["votes"].values() if v * 2 > votes)
    mine = sum(1 for i in ours if judged["votes"][i] * 2 > votes)
    return _gate(mine >= fixed - JUDGED_TOLERANCE, f"{mine}/{len(ours)} judged correct; the fixed path {fixed}/{len(ours)} (tolerance {JUDGED_TOLERANCE})")


def _cost_gate(summary: dict, baseline: Mapping | None) -> dict:
    main_avg, fixed = summary["cost"]["main_avg_usd"], (baseline or {}).get("avg_cost_usd")
    if main_avg is None or not fixed:
        return _gate(None, "not evaluated: needs a baseline report and answered MAIN-split questions")
    return _gate(main_avg <= COST_RATIO_MAX * fixed, f"${main_avg:.4f} per question vs ${fixed:.4f} for the fixed path "
                                                    f"(ratio {main_avg / fixed:.2f}, limit {COST_RATIO_MAX:g})")


def _also_fails(summary: dict, baseline: Mapping | None) -> list[str]:
    """The mechanical failures the fixed path (the baseline report's ``failed_ids``) fails too: a gate the baseline itself misses is a
    question about the gate, not about the agent."""
    return sorted(set((baseline or {}).get("failed_ids") or []) & set(summary["mechanical"]["failed_ids"]))


def evaluate_gates(summary: dict, *, judged: dict | None = None, baseline: Mapping | None = None, items: list[dict] = (),
                   votes: int = 3) -> dict:
    """The plan section 3 gates over a summary. ``baseline`` is the fixed path's deployed-eval report."""
    cit, latency = summary["citation_validity"], summary["latency"]
    misattributed = _judged_failures(judged, [it["id"] for it in items if it["type"] == "misattribution"], votes)
    mechanical = _all_pass(summary["mechanical"])
    if also := _also_fails(summary, baseline):
        mechanical["detail"] += f" (the fixed path also fails: {', '.join(also)})"
    return {
        "complete": _gate(bool(summary["n"]) and not summary["missing_ids"],
                          f"{summary['n']} answered" + (f"; missing: {', '.join(summary['missing_ids'])}" if summary["missing_ids"] else "")),
        "no_errors": _ids_gate(summary["errors"], "runs ended in an error"),
        "mechanical": mechanical,
        "citation_validity": _gate(None if cit is None else cit == 1.0, "no runs" if cit is None else
                                   f"{cit:.3f}" + (f"; invalid: {', '.join(summary['invalid_citation_ids'])}" if summary["invalid_citation_ids"] else "")),
        "ungrounded_numbers": _ids_gate(summary["ungrounded_ids"], "answers with an ungrounded number"),
        "misattribution": _all_pass(summary["misattribution"], misattributed),
        "refusals": _all_pass(summary["refusals"]),
        "injection": _all_pass(summary["injection"]),
        "trajectory": _ids_gate(summary["trajectory"]["failed"], "trajectories failed"),
        "no_fallback": _ids_gate(summary["fallbacks"]["ids"], "runs fell back to the plain retrieval"),
        "limits_respected": _ids_gate(summary["limit_violations"], "runs broke a limit"),
        "spend_consistent": _ids_gate(summary["spend_inconsistent"], "runs with inconsistent spend"),
        "p95_latency": _gate(None if latency is None else latency["p95"] <= P95_LATENCY_MAX_S,
                             "no latencies" if latency is None else f"p95 {latency['p95']:.1f}s (limit {P95_LATENCY_MAX_S:g}s)"),
        "blended_cost": _cost_gate(summary, baseline),
        "judged_correctness": _judged_gate(summary, judged, baseline, list(items), votes),
    }


def score_agent_runs(rows: list[dict], items: list[dict], *, limits: AgentLimits, judge=None, votes: int = 3,
                     judge_model: str | None = None, as_of: str | None = None, baseline: Mapping | None = None) -> dict:
    """Score saved runs against their items: free checks always, the paid judge only when ``judge`` is given.

    Returns ``rows`` (per-run scores), ``summary``, ``judged`` (None without a judge), ``gates``, ``unevaluated_gates`` and
    ``clears_all_gates`` (every gate True: a gate that could not be evaluated is not a pass)."""
    by_id = {it["id"]: it for it in items}
    unknown = sorted({r["id"] for r in rows} - set(by_id))
    if unknown:
        raise ValueError(f"runs for unknown question(s): {', '.join(unknown)}")
    scored = [score_run(by_id[r["id"]], r, limits) for r in rows]
    judged = judge_agent_runs(rows, items, judge, votes=votes, judge_model=judge_model, as_of=as_of) if judge else None
    summary = summarize_scored(scored, items)
    gates = evaluate_gates(summary, judged=judged, baseline=baseline, items=items, votes=votes)
    also = _also_fails(summary, baseline)
    return {"rows": scored, "summary": summary, "judged": judged, "gates": gates,
            "unevaluated_gates": [g for g, v in gates.items() if v["passed"] is None],
            "clears_all_gates": all(v["passed"] is True for v in gates.values()),
            "baseline_also_fails": {"mechanical": also} if also else {}}


# --- the T2 estimate ------------------------------------------------------------------------------------------------------------

def _price(model: str) -> tuple[float, float]:
    if model not in KNOWN_PRICES_PER_MTOK:
        raise ValueError(f"no price for {model} in llm_shape.KNOWN_PRICES_PER_MTOK: refusing to estimate (or spend) blind")
    return KNOWN_PRICES_PER_MTOK[model]


def _tokens_usd(model: str, prompt_tokens: float, completion_tokens: float) -> float:
    per_in, per_out = _price(model)
    return (prompt_tokens * per_in + completion_tokens * per_out) / 1e6


def estimate_agent_run(items: list[dict], *, baseline: Mapping, limits: AgentLimits, planner_model: str, votes: int = 3,
                       draft_model: str = DEFAULT_DRAFT_MODEL, escalation_model: str = DEFAULT_ESCALATION_MODEL) -> dict:
    """What one run of ``items`` would cost, before anything is bought: a likely figure and a worst case, with the assumptions named.

    Writer: the fixed path's measured average (``baseline['avg_cost_usd']``, the deployed-path report) per MAIN question and
    ``AGENT_SPLIT_COST_FACTOR`` times that per agent-split question; worst case, a rejected cheap draft plus a strong rewrite of the largest
    context on every question. Planner: ``PLANNER_CALLS_LIKELY`` calls of ``PLANNER_PROMPT_TOKENS`` in / ``PLANNER_COMPLETION_TOKENS`` out
    at the planner's own price (worst: ``limits.max_model_calls`` calls, each ``PLANNER_WORST_CASE_FACTOR`` times bigger). Judge: the
    conservative ``JUDGE_CALL_USD`` per vote for every judged question, in both figures."""
    fixed = baseline.get("avg_cost_usd")
    if not fixed:
        raise ValueError("the baseline report has no avg_cost_usd: cannot estimate the writer spend")
    n = len(items)
    n_main = sum(1 for it in items if it.get("split") == MAIN)
    n_judged = sum(1 for it in items if is_judged(it))
    per_call = _tokens_usd(planner_model, PLANNER_PROMPT_TOKENS, PLANNER_COMPLETION_TOKENS)
    worst_answer = _tokens_usd(escalation_model, WORST_ANSWER_PROMPT_TOKENS, WORST_ANSWER_COMPLETION_TOKENS)
    if draft_model != escalation_model:      # one model in both roles is plain live streaming: there is no draft to reject
        worst_answer += _tokens_usd(draft_model, WORST_ANSWER_PROMPT_TOKENS, WORST_ANSWER_COMPLETION_TOKENS)
    answers_likely = fixed * (n_main + (n - n_main) * AGENT_SPLIT_COST_FACTOR)
    planner_likely = n * PLANNER_CALLS_LIKELY * per_call
    planner_worst = n * limits.max_model_calls * per_call * PLANNER_WORST_CASE_FACTOR
    judge = n_judged * votes * JUDGE_CALL_USD
    return {"questions": n, "main_questions": n_main, "judged_questions": n_judged, "votes": votes,
            "answers_usd_likely": answers_likely, "answers_usd_worst_case": n * worst_answer,
            "planner_usd_likely": planner_likely, "planner_usd_worst_case": planner_worst, "judge_usd_worst_case": judge,
            "total_likely_usd": answers_likely + planner_likely + judge,
            "total_worst_case_usd": n * worst_answer + planner_worst + judge,
            "assumptions": {"writer_usd_per_main_question": fixed, "agent_split_cost_factor": AGENT_SPLIT_COST_FACTOR,
                            "planner_model": planner_model, "planner_calls_likely": PLANNER_CALLS_LIKELY,
                            "planner_calls_max": limits.max_model_calls, "planner_tokens_per_call": [PLANNER_PROMPT_TOKENS, PLANNER_COMPLETION_TOKENS],
                            "worst_case_answer_tokens": [WORST_ANSWER_PROMPT_TOKENS, WORST_ANSWER_COMPLETION_TOKENS],
                            "worst_case_answer_models": [draft_model, escalation_model], "judge_usd_per_vote": JUDGE_CALL_USD}}


# --- presentation (pure, so the CLI's output is tested without a terminal) ------------------------------------------------------

def plan_line(run_set: list[dict]) -> str:
    n, n_main = len(run_set), sum(1 for it in run_set if it.get("split") == MAIN)
    return f"run set: {n} question{'s' if n != 1 else ''} ({n - n_main} agent + {n_main} main)"


def describe_plan(run_set: list[dict]) -> list[str]:
    """What a run would ask, by split and category (or type, for the main benchmark): the whole output of ``--dry-run`` beyond the estimate."""
    groups: dict[tuple[str, str], list[str]] = {}
    for it in run_set:
        groups.setdefault((it.get("split", AGENT), it.get("category") or it["type"]), []).append(it["id"])
    return [f"  {split:5} {name:15} {len(ids):3}  " + (", ".join(ids) if len(ids) <= 8 else f"{ids[0]} .. {ids[-1]}")
            for (split, name), ids in groups.items()]


def format_estimate(est: Mapping) -> list[str]:
    return [f"estimate (before any spend): {est['questions']} questions; the judge is paid for {est['judged_questions']} of them x {est['votes']} votes",
            f"  answers  likely ${est['answers_usd_likely']:.2f}   worst case ${est['answers_usd_worst_case']:.2f}",
            f"  planner  likely ${est['planner_usd_likely']:.3f}   worst case ${est['planner_usd_worst_case']:.3f}",
            f"  judge    worst case ${est['judge_usd_worst_case']:.2f}",
            f"  TOTAL    likely ${est['total_likely_usd']:.2f}   worst case ${est['total_worst_case_usd']:.2f}   (assumptions: see the report)"]


def format_gates(result: Mapping) -> list[str]:
    tag = {True: "PASS", False: "FAIL", None: "n/a "}
    return [f"  {tag[v['passed']]} {name:20} {v['detail']}" for name, v in result["gates"].items()]

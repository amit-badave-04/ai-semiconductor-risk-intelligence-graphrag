"""S12: the pre-M5a live latency smoke (docs/v2/M5_PLAN.md section 5, window W2): the calibration runner of the mock LLM.

Streams a few REAL asks (default 10 SEC + 2 agent) through the async answer twin with the LIVE model strings, against the
LOCAL graph, and records for each model call the time to first token, the decode time, the chunk count, the visible length
and the billed usage; and for each ask the route, the escalation, the retrieval time and the cost. The JSON it writes is what
``tools/mockllm/calibrate.py --s12`` reads to replace the provisional numbers of ``tools/mockllm/profiles.json``.

    uv run python scripts/latency_smoke.py --dry-run                 # the plan and the cost bound; no provider, no graph, no network
    uv run python scripts/latency_smoke.py --live --max-usd 0.50     # PAID. Only with the owner's go for window W2.

One of ``--dry-run`` and ``--live`` is required: there is no default that spends money. The dry run builds ``Settings`` from
its arguments only (``_env_file=None``), never loads ``.env``, never opens a driver, an embedder or a socket, and calls no
provider. It does import LiteLLM (through the service's cost ceilings, ``serve/estimate``, which read the retriever's anchor
cap); with ``LITELLM_LOCAL_MODEL_COST_MAP`` forced on at the top of this file that import fetches nothing.

HARD COST CAP (``--max-usd``, default 0.50), enforced at two levels.

* Per ask: before each ask the runner retrieves locally (free: embedding + graph reads), renders the real prompt and computes
  THIS ask's worst case: the draft model once, the strong model twice (the service's ``estimate.DRAFT_ATTEMPTS`` /
  ``STREAM_ATTEMPTS``), each billed for the whole prompt (its characters over THAT MODEL's characters per token,
  ``estimate.chars_per_token``: 2.0 for Claude Sonnet 5, 2.5 for the rest) plus the full output budget, at the real prices.
  The ask starts only if ``spent + worst case <= max-usd``. An agent ask builds its prompt inside the agent, so this check
  reserves it at ``--agent-prompt-tokens`` (default 60,000; the largest of the 82 recorded agent asks was 47,683 tokens),
  which is a measured figure, not a code cap; a token figure is read on the densest model (``reference_chars``).
* Per model call: every call (a draft or strong stream, a planner call) computes its OWN worst case from the prompt it is
  about to send and is refused, before any request leaves the process, unless ``spent + the worst cases of the calls already
  started in this ask + this call <= max-usd``. A refused draft becomes a ``draft_error`` (the service then escalates, and
  that call is refused too), a refused planner call becomes the plain-retrieval fallback: the ask ends with an error or a plain
  answer and nothing is spent. This is what makes the cap hard for agent asks, whose prompt size is not known beforehand.

``spent`` is what the finished asks reported (``cost_usd``), or, when one reported none, the worst case of the calls it had
started. So a run crosses the cap only if a provider bills more than a call's worst case (the estimate's own known gaps: a
prompt denser than the assumed characters per token, an assumption below the lowest EXACT recorded row and not a proof, and a
failed request the provider bills anyway; Claude Sonnet 5 has no long-context price tier, see ``serve/estimate``). The
recorded asks cost $0.0146 on average and $0.07 at most.

A live run needs the local graph up (``NEO4J_URI`` must be a localhost address, or the script refuses) and the local embedder
(``EMBEDDING_BACKEND`` from ``.env``; the torch model loads in this process), plus the provider keys in ``.env``.

Output: ``artifacts/s12_latency_smoke.json`` (its own file: ``artifacts/m5_spikes.json`` is merged by the owner of that file).
Questions are identified by id, never by text; answers are not stored.
"""

import argparse
import asyncio
import json
import math
import os
import sys
import time
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"     # forced, before any semigraph import: LiteLLM never fetches its cost map

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "artifacts" / "s12_latency_smoke.json"
FLY_TOML = ROOT / "fly.toml"
AGENT_BENCHMARK = ROOT / "artifacts" / "agent_benchmark.json"
DEFAULT_MAX_USD = 0.50
DEFAULT_SEC, DEFAULT_AGENT = 10, 2
AGENT_PROMPT_TOKENS = 60_000          # see the module doc
ASK_TIMEOUT_S = 180.0
DEFAULT_MODELS = {"draft": "openai/gpt-6-luna", "strong": "anthropic/claude-sonnet-5", "planner": "openai/gpt-6-luna"}
DEFAULT_ANSWER_MAX_TOKENS = 2400      # fly.toml LLM_ANSWER_MAX_TOKENS
EXCLUDED_AGENT_CATEGORIES = ("injection",)          # a latency smoke is not a security test
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
# Reference prompt sizes for the dry run's arithmetic (billed prompt tokens of the 60 recorded deployed-path asks).
RECORDED_PROMPT_TOKENS = {"median": 13_730, "max": 38_796}
RECORDED_ASK_USD = {"mean": 0.0146, "max": 0.0707}
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Models:
    draft: str
    strong: str
    planner: str


@dataclass(frozen=True)
class LiveConfig:
    models: Models
    answer_max_tokens: int


@dataclass(frozen=True)
class PlannedAsk:
    id: str
    kind: str              # sec | agent
    qtype: str
    question: str


@dataclass(frozen=True)
class Plan:
    asks: tuple[PlannedAsk, ...]
    max_usd: float
    agent_prompt_tokens: int = AGENT_PROMPT_TOKENS
    ask_timeout_s: float = ASK_TIMEOUT_S


@dataclass(frozen=True)
class Prepared:
    """One ask, ready to be bought: what it can cost at most, what retrieval cost in time, and the event stream to run."""

    reserve_usd: float
    prompt_chars: int | None
    embed_s: float | None                 # None = not measured (an agent ask retrieves inside the agent)
    retrieval_s: float | None
    events: Callable[[list], AsyncIterator[dict]]          # sink (for the model-call records) -> the ask's events


class BudgetRefused(Exception):
    """A model call whose worst case does not fit under the cap: refused before any request is made."""


class Budget:
    """The hard cap, per model call. ``spent`` = asks settled; ``pending`` = worst cases of the calls started in the running ask."""

    def __init__(self, max_usd: float):
        self.max_usd, self.spent, self.pending, self.calls, self.refused = max_usd, 0.0, 0.0, 0, 0

    def room(self) -> float:
        return self.max_usd - self.spent - self.pending

    def commit(self, worst_case_usd_: float) -> bool:
        """Reserve one call's worst case; False (nothing reserved) if it does not fit."""
        if worst_case_usd_ > self.room() + 1e-12:
            self.refused += 1
            return False
        self.pending += worst_case_usd_
        self.calls += 1
        return True

    def settle(self, reported_usd: float | None, fallback_usd: float) -> float:
        """Close the ask: charge what it reported, else what its started calls could have cost (``fallback_usd`` when no
        call went through the budget at all, e.g. a test double); returns the charge."""
        charged = reported_usd if reported_usd is not None else (self.pending if self.calls else fallback_usd)
        self.spent += charged
        self.pending, self.calls, self.refused = 0.0, 0, 0
        return charged


# --- configuration, questions, plan -----------------------------------------------------------------------------------

def live_config(fly_toml: Path = FLY_TOML) -> LiveConfig:
    """The live model strings and the answer budget: ``[env]`` of ``fly.toml`` (not secret), else the documented defaults."""
    env = {}
    if fly_toml.is_file():
        env = tomllib.loads(fly_toml.read_text(encoding="utf-8")).get("env", {})
    models = Models(env.get("ANSWER_MODEL") or DEFAULT_MODELS["draft"], env.get("ESCALATION_MODEL") or DEFAULT_MODELS["strong"],
                    env.get("AGENT_PLANNER_MODEL") or DEFAULT_MODELS["planner"])
    return LiveConfig(models, int(env.get("LLM_ANSWER_MAX_TOKENS") or DEFAULT_ANSWER_MAX_TOKENS))


def price_settings(config: LiveConfig):
    """``Settings`` for pricing only: built from arguments, never from ``.env``."""
    from semigraph.config import Settings
    return Settings(_env_file=None, answer_model=config.models.draft, escalation_model=config.models.strong,
                    agent_planner_model=config.models.planner, llm_answer_max_tokens=config.answer_max_tokens)


def spread(items: Sequence[dict], key: str, n: int) -> list[dict]:
    """``n`` items taken round-robin across the groups of ``key`` (largest group first, ids in order): a mix, deterministic."""
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item[key], []).append(item)
    ordered = sorted(groups.values(), key=lambda g: (-len(g), g[0][key]))
    picked: list[dict] = []
    for depth in range(max((len(g) for g in ordered), default=0)):
        picked += [g[depth] for g in ordered if depth < len(g)]
    return picked[:n]


def select_asks(n_sec: int, n_agent: int) -> tuple[PlannedAsk, ...]:
    """The agent asks first (the dearest to skip), then the SEC asks."""
    from semigraph.artifacts import load_benchmark
    agent_all = json.loads(AGENT_BENCHMARK.read_text(encoding="utf-8"))["questions"]
    agent = spread([q for q in agent_all if q["category"] not in EXCLUDED_AGENT_CATEGORIES], "category", n_agent)
    sec = spread(load_benchmark(), "type", n_sec)
    return (tuple(PlannedAsk(q["id"], "agent", q["category"], q["q"]) for q in agent)
            + tuple(PlannedAsk(q["id"], "sec", q["type"], q["q"]) for q in sec))


def worst_case_usd(prompt_chars: int, settings, *, planner: bool = False) -> float:
    """What one ask can cost at most, by the service's own arithmetic (``serve/estimate``): the draft model's attempts and the
    strong model's attempts, each for a prompt of ``prompt_chars`` CHARACTERS and the full output budget; plus the planner's
    calls for an agent ask. The characters are turned into tokens PER MODEL, exactly as the service does
    (``estimate.tokens_for_chars``: Claude Sonnet 5 at 2.0 characters per token, every other model at 2.5), because one token
    count cannot be right for both: the same prompt is more tokens on Sonnet than on Luna. The components ARE the service's
    (``estimate._answer_components``), so a test can pin this to ``estimate.estimate`` at ``estimate.prompt_chars``."""
    from semigraph.serve import estimate as E
    parts = E._answer_components(prompt_chars, max(0, int(settings.llm_answer_max_tokens)), settings)
    tokens_in = max(c.input_tokens for c in parts)
    if planner:
        parts.append(E._planner_component(settings))
    return E.AskEstimate("smoke", tokens_in, 0, tuple(parts)).usd


def call_worst_case_usd(role: str, prompt_chars: int, settings) -> float:
    """What ONE model call can cost at most: a ``draft`` stream (one attempt), a ``strong`` stream (two attempts, the
    stream's own retry of an empty reply) or one ``planner`` call (the planner's own prompt ceiling; ``prompt_chars`` is
    ignored). The same components as :func:`worst_case_usd`, one role at a time, and the prompt's characters are sized by THE
    CALLED MODEL's ratio: a Sonnet call is 2.0 characters per token, not the 2.5 of Luna (at 2.5 its guard was 20% too low)."""
    from semigraph.serve import estimate as E
    out = max(0, int(settings.llm_answer_max_tokens))
    if role == "planner":
        return E.AskEstimate("smoke", 0, 0, (replace(E._planner_component(settings), calls=1),)).usd
    model, attempts = ((settings.answer_model, E.DRAFT_ATTEMPTS) if role == "draft" else (settings.escalation_model, E.STREAM_ATTEMPTS))
    part = E._component(role, model, role, attempts, prompt_chars, out, settings)
    return E.AskEstimate("smoke", part.input_tokens, 0, (part,)).usd


def reference_chars(prompt_tokens: int) -> int:
    """Characters standing for ``prompt_tokens`` BILLED tokens: the recorded prompt sizes and ``--agent-prompt-tokens`` are
    token counts, and the worst case takes characters. They are read on the DENSEST model the service assumes (Sonnet's 2.0
    characters per token), so the dearest model's component is priced at exactly those tokens and a cheaper model's at fewer
    (Luna reads the same prompt as 0.8 of them)."""
    from semigraph.serve import estimate as E
    densest = min(E.DEFAULT_CHARS_PER_TOKEN, *E.CHARS_PER_TOKEN_BY_MODEL.values())
    return math.ceil(Decimal(prompt_tokens) * densest)


def reference_worst_case_usd(prompt_tokens: int, settings, *, planner: bool = False) -> float:
    """:func:`worst_case_usd` for a prompt given in billed tokens (see :func:`reference_chars`)."""
    return worst_case_usd(reference_chars(prompt_tokens), settings, planner=planner)


def reference_reserves(plan: Plan, settings) -> list[float]:
    """Each planned ask's worst case at the largest RECORDED prompt (SEC) or at ``plan.agent_prompt_tokens`` (agent)."""
    return [reference_worst_case_usd(plan.agent_prompt_tokens if a.kind == "agent" else RECORDED_PROMPT_TOKENS["max"], settings,
                                     planner=a.kind == "agent") for a in plan.asks]


def service_ceilings(plan: Plan, settings) -> list[float]:
    """Each planned ask's ceiling by the service's own caps (``estimate.estimate``: the retrieval caps, not a measurement)."""
    from semigraph.serve import estimate as E
    return [E.estimate("agent" if a.kind == "agent" else "hybrid", settings).usd for a in plan.asks]


def admitted_if_every_ask_cost_its_worst_case(reserves: Sequence[float], max_usd: float) -> int:
    spent, n = 0.0, 0
    for reserve in reserves:
        if spent + reserve <= max_usd:
            spent += reserve
            n += 1
    return n


def cost_bound_usd(ceilings: Sequence[float], max_usd: float) -> float:
    """The most the run can spend: never more than ``max_usd`` (every model call is refused unless its worst case fits under
    it) and never more than the sum of the asks' ceilings by the service's own caps (no ask can cost more than that)."""
    return min(max_usd, sum(ceilings))


def describe(plan: Plan, config: LiveConfig, settings) -> list[str]:
    reserves = reference_reserves(plan, settings)
    sec = [a for a in plan.asks if a.kind == "sec"]
    lines = ["S12 latency smoke: DRY RUN (no provider call, no graph, no embedder, no network, .env not read)",
             f"models (fly.toml [env]): draft={config.models.draft} strong={config.models.strong} planner={config.models.planner}; "
             f"answer budget {config.answer_max_tokens} tokens",
             f"asks: {len(plan.asks)} = {len(sec)} SEC (async twin, hybrid) + {len(plan.asks) - len(sec)} agent; agent asks first"]
    lines += [f"  {i:02d} {a.kind:5s} {a.id:5s} {a.qtype}" for i, a in enumerate(plan.asks, 1)]
    ceilings = service_ceilings(plan, settings)
    lines += [
        "guard 1, per ask (live run): retrieve locally for free, render the real prompt, compute that ask's worst case; start the "
        "ask only if spent + worst case <= max-usd (an agent ask is reserved at --agent-prompt-tokens, a measured figure)",
        "guard 2, per model call: each draft, strong and planner call computes its own worst case from the prompt it is about to "
        "send and is refused before any request unless spent + the calls started in this ask + this call <= max-usd",
        f"worst case of one SEC ask: ${reference_worst_case_usd(RECORDED_PROMPT_TOKENS['median'], settings):.4f} at the recorded median "
        f"prompt ({RECORDED_PROMPT_TOKENS['median']:,} tokens), ${reference_worst_case_usd(RECORDED_PROMPT_TOKENS['max'], settings):.4f} "
        f"at the recorded max ({RECORDED_PROMPT_TOKENS['max']:,}); an agent ask is reserved at "
        f"${reference_worst_case_usd(plan.agent_prompt_tokens, settings, planner=True):.4f} ({plan.agent_prompt_tokens:,} tokens + the "
        "planner); token figures are read on the densest model (Sonnet, 2.0 characters per token), each model then sizes the prompt by its own ratio",
        f"the service's own ceiling per ask (serve/estimate, from its retrieval caps): SEC ${ceilings[-1] if sec else 0:.4f}"
        + (f", agent ${ceilings[0]:.4f}" if len(sec) < len(plan.asks) else ""),
        f"recorded asks cost ${RECORDED_ASK_USD['mean']:.4f} on average and ${RECORDED_ASK_USD['max']:.4f} at most: "
        f"expected spend about ${RECORDED_ASK_USD['mean'] * len(plan.asks):.2f} for {len(plan.asks)} asks",
        f"asks guard 1 would admit if EVERY ask cost its worst case at the reference prompts above: "
        f"{admitted_if_every_ask_cost_its_worst_case(reserves, plan.max_usd)} of {len(plan.asks)} (real asks cost far less, so the "
        "guard releases the rest as the spend is reported)",
        f"COST BOUND (hard): ${cost_bound_usd(ceilings, plan.max_usd):.6f} <= --max-usd ${plan.max_usd:.2f}",
        "nothing was called; run with --live (and the owner's go for window W2) to spend"]
    return lines


# --- the model-call records --------------------------------------------------------------------------------------------

def timed_stream_class():
    """``AsyncTextStream`` that records each model call (imported lazily: the dry run must not load LiteLLM)."""
    from semigraph.retrieval.answerer_async import AsyncTextStream

    class TimedStream(AsyncTextStream):
        def __init__(self, prompt, *, sink: list, role: str, **kwargs):
            super().__init__(prompt, **kwargs)
            self._sink, self._role = sink, role

        async def __aiter__(self):
            started = time.monotonic()
            first = last = None
            chunks = chars = 0
            try:
                async with aclosing(super().__aiter__()) as inner:
                    async for delta in inner:
                        last = time.monotonic()
                        first = last if first is None else first
                        chunks += 1
                        chars += len(delta)
                        yield delta
            finally:
                usage = self.usage or {}
                self._sink.append({"role": self._role, "model": self.model,
                                   "ttft_s": None if first is None else round(first - started, 4),
                                   "decode_s": 0.0 if first is None else round(last - first, 4), "n_deltas": chunks,
                                   "visible_chars": chars, "prompt_tokens": usage.get("prompt_tokens"),
                                   "completion_tokens": usage.get("completion_tokens"),
                                   "usage_estimated": bool(usage.get("estimated")), "finish_reason": self.finish_reason})

    return TimedStream


class RefusedStream:
    """A model call the budget refused: iterating it raises before any request is made (the answer path reads that as a failed
    draft, or as an ``error`` event for the strong model)."""

    def __init__(self, model: str):
        self.model, self.usage, self.finish_reason, self.text = model, None, None, ""

    def __aiter__(self):
        return self._refuse()

    async def _refuse(self):
        raise BudgetRefused(f"the call to {self.model} was refused: its worst case does not fit under --max-usd")
        yield ""        # unreachable: makes this an async generator


class TimedPlanner:
    """Wraps the agent's planner, records each call (a planner call is not streamed: its whole latency is the figure) and, with a
    ``budget``, refuses a call whose worst case does not fit (the agent then falls back to the plain retrieval)."""

    def __init__(self, inner, sink: list, budget: Budget | None = None, call_worst_usd: float = 0.0):
        self._inner, self._sink, self._budget, self._worst = inner, sink, budget, call_worst_usd

    def __call__(self, messages, tools, *, timeout):
        if self._budget is not None and not self._budget.commit(self._worst):
            raise BudgetRefused("a planner call was refused: its worst case does not fit under --max-usd")
        started = time.monotonic()
        record = {"role": "planner", "model": getattr(self._inner, "model", None), "latency_s": None, "completion_tokens": None,
                  "prompt_tokens": None}
        try:
            turn = self._inner(messages, tools, timeout=timeout)
            usage = turn.usage or {}
            record.update(prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))
            return turn
        finally:
            record["latency_s"] = round(time.monotonic() - started, 4)
            self._sink.append(record)


# --- running the plan ---------------------------------------------------------------------------------------------------

@dataclass
class Outcome:
    terminal: str = "none"                 # done | error | timeout | failed
    client_ttfb_s: float | None = None
    total_s: float = 0.0
    cost_usd: float | None = None
    routed: str | None = None
    escalated: bool | None = None
    escalation_reasons: list | None = None
    checks_failed: list | None = None
    failure: str | None = None


async def drain(events: AsyncIterator[dict], timeout_s: float, clock: Callable[[], float] = time.monotonic) -> Outcome:
    """Run one ask's events to the end: when the first delta reached the client, the terminal event, a timeout or a crash."""
    out, started = Outcome(), clock()
    try:
        async with asyncio.timeout(timeout_s), aclosing(events) as stream:
            async for event in stream:
                if event.get("event") == "delta" and out.client_ttfb_s is None:
                    out.client_ttfb_s = round(clock() - started, 4)
                if event.get("event") in ("done", "error"):
                    _read_terminal(out, event)
    except TimeoutError:
        out.terminal = "timeout"
    except Exception as e:  # noqa: BLE001 - one failed ask must not end the smoke; the class name only, never the message
        out.terminal, out.failure = "failed", type(e).__name__
    out.total_s = round(clock() - started, 4)
    return out


def _read_terminal(out: Outcome, event: dict) -> None:
    from semigraph.retrieval.verify import failed_check_names
    out.terminal = event["event"]
    out.cost_usd = event.get("cost_usd") if isinstance(event.get("cost_usd"), (int, float)) else None
    out.routed, out.escalated = event.get("routed"), event.get("escalated")
    out.escalation_reasons = event.get("escalation_reasons")
    out.checks_failed = failed_check_names(event.get("checks"))
    if event["event"] == "error":
        out.failure = "stream error event"


def ask_record(ask: PlannedAsk, prepared: Prepared | None, out: Outcome | None, *, status: str, charged: float,
               refused: int = 0) -> dict:
    record = {"id": ask.id, "kind": ask.kind, "qtype": ask.qtype, "status": status, "charged_usd": round(charged, 6),
              "reserved_usd": None if prepared is None else round(prepared.reserve_usd, 6), "calls_refused": refused}
    if prepared is not None:
        record.update(prompt_chars=prepared.prompt_chars, embed_s=prepared.embed_s, retrieval_s=prepared.retrieval_s)
    if out is not None:
        record.update(terminal=out.terminal, routed=out.routed, escalated=out.escalated, escalation_reasons=out.escalation_reasons,
                      client_ttfb_s=out.client_ttfb_s, total_s=out.total_s, cost_usd=out.cost_usd,
                      checks_failed=out.checks_failed, failure=out.failure)
    return record


async def run_plan(plan: Plan, prepare: Callable[[PlannedAsk, Budget], Awaitable[Prepared]]) -> dict:
    """Buy the plan's asks one at a time under the cost guard. ``prepare(ask, budget)`` retrieves (free) and returns a
    :class:`Prepared`; the ask is started only if its worst case fits under ``max_usd`` next to what is already spent, and the
    ``budget`` it was given refuses any single model call that would not fit."""
    budget = Budget(plan.max_usd)
    asks, calls = [], []
    for ask in plan.asks:
        try:
            prepared = await prepare(ask, budget)
        except Exception as e:  # noqa: BLE001 - retrieval failed: nothing was bought
            asks.append({"id": ask.id, "kind": ask.kind, "qtype": ask.qtype, "status": "preparation_failed",
                         "failure": type(e).__name__, "charged_usd": 0.0})
            continue
        if budget.spent + prepared.reserve_usd > plan.max_usd:
            asks.append(ask_record(ask, prepared, None, status="skipped_cost_guard", charged=0.0))
            continue
        sink: list = []
        out = await drain(prepared.events(sink), plan.ask_timeout_s)
        refused = budget.refused
        charged = budget.settle(out.cost_usd, prepared.reserve_usd)
        asks.append(ask_record(ask, prepared, out, status="ran", charged=charged, refused=refused))
        calls += [{"ask": ask.id, **c} for c in sink]
    return document(plan, asks, calls, budget.spent)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)], 4)


def summary(asks: list[dict], calls: list[dict], spent: float) -> dict:
    ran = [a for a in asks if a["status"] == "ran"]
    cheap = [a for a in ran if a.get("routed") == "cheap"]
    out = {"spent_usd": round(spent, 6), "asks_run": len(ran), "asks_skipped": len(asks) - len(ran),
           "cheap_routed": len(cheap), "escalated": sum(1 for a in cheap if a.get("escalated")),
           "escalation_rate": (sum(1 for a in cheap if a.get("escalated")) / len(cheap)) if cheap else None}
    for role in ("draft", "strong"):
        ttft = [c["ttft_s"] for c in calls if c["role"] == role and c.get("ttft_s") is not None]
        out[f"{role}_ttft_s"] = {"n": len(ttft), "p50": _quantile(ttft, 0.5), "p95": _quantile(ttft, 0.95)}
    return out


def document(plan: Plan, asks: list[dict], calls: list[dict], spent: float) -> dict:
    return {"spike": "S12", "version": SCHEMA_VERSION, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "plan": {"asks": len(plan.asks), "max_usd": plan.max_usd, "agent_prompt_tokens": plan.agent_prompt_tokens},
            "asks": asks, "calls": calls, "summary": summary(asks, calls, spent)}


# --- the live runtime (retrieval, prompt, streams) -----------------------------------------------------------------------------

class LiveRuntime:
    """Prepares asks against a driver and an embedder (the real local ones, or fakes in the test)."""

    def __init__(self, driver, embedder, limiters, settings, config: LiveConfig, agent_prompt_tokens: int = AGENT_PROMPT_TOKENS):
        self.driver, self.embedder, self.limiters, self.settings, self.config = driver, embedder, limiters, settings, config
        self.agent_prompt_tokens = agent_prompt_tokens
        self.price_settings = price_settings(config)
        self.timed = timed_stream_class()

    async def prepare(self, ask: PlannedAsk, budget: Budget) -> Prepared:
        return await (self._prepare_agent(ask, budget) if ask.kind == "agent" else self._prepare_sec(ask, budget))

    def _factories(self, sink_ref: list, budget: Budget):
        """``llm_stream`` / ``escalation_stream`` factories that time every model call into ``sink_ref[0]`` and refuse a call
        whose own worst case (from the prompt it is about to send) does not fit under the cap."""
        from semigraph.retrieval.answerer import _draft_kwargs
        kw = {"max_tokens": self.config.answer_max_tokens, "timeout": self.settings.llm_request_timeout_s}
        m = self.config.models

        def guarded(role: str, model: str, make: Callable[[str], object]) -> Callable[[str], object]:
            def factory(prompt: str):
                worst = call_worst_case_usd(role, len(prompt), self.price_settings)
                return make(prompt) if budget.commit(worst) else RefusedStream(model)
            return factory

        draft = guarded("draft", m.draft, lambda prompt: self.timed(
            prompt, sink=sink_ref[0], role="draft", **_draft_kwargs({"model": m.draft, **kw})))
        strong = guarded("strong", m.strong, lambda prompt: self.timed(prompt, sink=sink_ref[0], role="strong", model=m.strong, **kw))
        return draft, strong

    async def _prepare_sec(self, ask: PlannedAsk, budget: Budget) -> Prepared:
        import anyio

        from semigraph.retrieval.answerer import build_blocks, render_prompt
        from semigraph.retrieval.answerer_async import astream_answer_for_context
        from semigraph.retrieval.retriever import hybrid_retrieve
        t0 = time.monotonic()
        vec = await anyio.to_thread.run_sync(self.embedder.encode_query, ask.question, limiter=self.limiters.embed)
        t1 = time.monotonic()
        r = await anyio.to_thread.run_sync(partial(hybrid_retrieve, ask.question, self.driver, self.embedder, k_chunks=8, hops=2,
                                                   query_vec=vec), limiter=self.limiters.db)
        t2 = time.monotonic()
        prompt = render_prompt(ask.question, build_blocks(r)[0])
        reserve = worst_case_usd(len(prompt), self.price_settings)
        sink_ref: list = [[]]
        draft, strong = self._factories(sink_ref, budget)
        m = self.config.models

        def events(sink: list) -> AsyncIterator[dict]:
            sink_ref[0] = sink
            return astream_answer_for_context(
                ask.question, r, "hybrid", llm_stream=draft, escalation_model=m.strong, escalation_stream=strong,
                limiters=self.limiters, max_tokens=self.config.answer_max_tokens, timeout=self.settings.llm_request_timeout_s,
                model=m.draft)
        return Prepared(reserve, len(prompt), round(t1 - t0, 4), round(t2 - t1, 4), events)

    async def _prepare_agent(self, ask: PlannedAsk, budget: Budget) -> Prepared:
        from semigraph.agent.planner import LiteLLMPlanner
        from semigraph.agent.stream_async import aagent_answer_stream
        reserve = reference_worst_case_usd(self.agent_prompt_tokens, self.price_settings, planner=True)
        sink_ref: list = [[]]
        draft, strong = self._factories(sink_ref, budget)
        m = self.config.models
        agent_settings = self.settings.model_copy(update={"agent_planner_model": m.planner})
        planner_worst = call_worst_case_usd("planner", 0, self.price_settings)

        def events(sink: list) -> AsyncIterator[dict]:
            sink_ref[0] = sink
            return aagent_answer_stream(
                ask.question, self.driver, self.embedder, strategy="agent", limiters=self.limiters,
                timeout=self.settings.llm_request_timeout_s, max_tokens=self.config.answer_max_tokens, escalation_model=m.strong,
                settings=agent_settings, planner=TimedPlanner(LiteLLMPlanner(m.planner), sink, budget, planner_worst), llm_stream=draft,
                escalation_stream=strong, model=m.draft)
        return Prepared(reserve, None, None, None, events)


def require_local_graph(settings) -> None:
    host = urlparse(settings.neo4j_uri).hostname or ""
    if host not in LOCAL_HOSTS:
        raise SystemExit("refused: NEO4J_URI is not a local address; S12 runs against the LOCAL graph only")


async def run_live(plan: Plan, config: LiveConfig) -> dict:
    """The real run: local driver and embedder, the app's own limiters. Provider keys come from ``.env`` through LiteLLM's
    environment lookup, exactly as the other live scripts do; no value is printed."""
    from semigraph.config import get_settings
    from semigraph.embeddings import Embedder
    from semigraph.graph.client import get_driver
    from semigraph.serve.limiters import make_limiters
    settings = get_settings()
    require_local_graph(settings)
    driver = get_driver(settings)
    try:
        runtime = LiveRuntime(driver, Embedder(), make_limiters(settings), settings, config, plan.agent_prompt_tokens)
        return await run_plan(plan, runtime.prepare)
    finally:
        driver.close()


# --- command line -----------------------------------------------------------------------------------------------------------

def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the plan and the cost bound; call nothing")
    mode.add_argument("--live", action="store_true", help="PAID: stream the asks through the real models")
    p.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD, help="hard cap on the total spend (default %(default)s)")
    p.add_argument("--sec", type=int, default=DEFAULT_SEC, help="SEC asks (default %(default)s)")
    p.add_argument("--agent", type=int, default=DEFAULT_AGENT, help="agent asks (default %(default)s)")
    p.add_argument("--agent-prompt-tokens", type=int, default=AGENT_PROMPT_TOKENS, help="reservation for an agent ask's prompt")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)
    if not (math.isfinite(args.max_usd) and args.max_usd > 0) or args.sec < 0 or args.agent < 0 or args.sec + args.agent < 1:
        p.error("--max-usd must be positive and at least one ask must be requested")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = live_config()
    plan = Plan(select_asks(args.sec, args.agent), args.max_usd, args.agent_prompt_tokens)
    if args.dry_run:
        print("\n".join(describe(plan, config, price_settings(config))))
        return 0
    from dotenv import load_dotenv
    load_dotenv()                      # provider keys into the environment for LiteLLM; never printed
    doc = asyncio.run(run_live(plan, config))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes((json.dumps(doc, indent=1) + "\n").encode("utf-8"))                    # LF on every platform
    s = doc["summary"]
    print(f"S12 done: {s['asks_run']} asks run, {s['asks_skipped']} skipped, spent ${s['spent_usd']:.4f} of ${plan.max_usd:.2f}; wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

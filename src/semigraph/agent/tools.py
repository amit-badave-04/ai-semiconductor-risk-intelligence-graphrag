"""The seven read-only tools of the retrieval planner (docs/v2/M3_AGENT_PLAN.md section 1).

``lookup_company``, ``search_filings``, ``financial_metrics``, ``risk_changes``, ``relationships``, ``active_risks`` and
``compute_change``. Each one

- takes a validated pydantic model (``extra="forbid"``; the JSON schemas the planner sees are generated from the same models);
- runs ONLY the retriever's own parameterised queries (``retrieval.retriever`` constants and its ``select_*`` functions): there is
  no text-to-Cypher anywhere on this path, and no tool has an argument that could carry a query;
- merges what it found into the retrieval dict with the pure functions of :mod:`semigraph.agent.merge` (it never mutates ``r``);
- returns a :class:`ToolOutcome` whose ``result`` (what the planner is told) is built from the allowlisted views of
  :mod:`semigraph.agent.sanitize`: counts, ids, fiscal years, metric values and universe company names, never filing text.

A tool never raises: a refused call (unknown tool, malformed or invalid arguments, unknown company, a computation the facts cannot
support) and a failure (a database or embedder error) both come back as ``{"error": ...}`` with ``ok=False``. Error results carry
fixed text plus, for a failure, the exception TYPE name only: a Neo4j error message echoes its parameters.
"""

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any

from neo4j import Query as _CypherQuery
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from ..retrieval import retriever as R
from . import merge as M
from . import sanitize as S

logger = logging.getLogger("semigraph.agent.tools")

TOOL_NAMES = ("lookup_company", "search_filings", "financial_metrics", "risk_changes", "relationships", "active_risks",
              "compute_change")
MAX_COMPANIES = 4
# The real vocabulary (S.KNOWN_METRICS), not a permissive [a-z_]+ shape: a metric name is never a free-text channel to
# the planner (M3 finding #8).
_METRIC_PATTERN = r"^(?:{})$".format("|".join(S.KNOWN_METRICS))


def _iso_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as e:
        raise ValueError("not a calendar date") from e
    return value


_Company = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
_Metric = Annotated[str, StringConstraints(pattern=_METRIC_PATTERN)]
_Year = Annotated[int, Field(ge=1990, le=2100)]
_IsoDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_iso_date)]


def _companies(description: str = "company names as in lookup_company") -> Any:
    return Field(min_length=1, max_length=MAX_COMPANIES, description=description)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LookupCompanyArgs(_Args):
    name: _Company = Field(description="a company name, alias or ticker")


class SearchFilingsArgs(_Args):
    query: str = Field(min_length=1, max_length=300, description="what to look for, in plain words")
    companies: list[_Company] = Field(default_factory=list, max_length=MAX_COMPANIES,
                                      description="only excerpts that mention these companies; empty = all filings")
    k: int = Field(6, ge=1, le=10, description="how many excerpts")


class FinancialMetricsArgs(_Args):
    companies: list[_Company] = _companies()
    metrics: list[_Metric] = Field(default_factory=list, max_length=8,
                                   description="only these metrics (revenue, net_income, rnd, capex); empty = all four")
    fiscal_years: list[_Year] = Field(default_factory=list, max_length=4,
                                      description="fiscal years by the year of the period end, in addition to the latest periods")
    period_ends: list[_IsoDate] = Field(default_factory=list, max_length=4,
                                        description="exact period-end dates (YYYY-MM-DD), in addition to the latest periods")


class RiskChangesArgs(_Args):
    companies: list[_Company] = _companies()
    fiscal_years: list[_Year] = Field(default_factory=list, max_length=5,
                                      description="compare the annual reports of these fiscal years; empty = the latest comparison")
    multi_year: bool = Field(False, description="compare the newest annual reports pairwise (for 'across recent annual reports')")


class RelationshipsArgs(_Args):
    companies: list[_Company] = _companies()
    hops: int = Field(1, ge=1, le=2, description="1 = direct supplier / customer / competitor links")


class ActiveRisksArgs(_Args):
    companies: list[_Company] = _companies()
    topic: str = Field("", max_length=200, description="what the risks should be about; empty = the question")


class ComputeChangeArgs(_Args):
    company: _Company = Field(description="company name as in lookup_company")
    metric: _Metric = Field(description="revenue, net_income, rnd or capex")
    from_period_end: _IsoDate = Field(description="period end of the earlier fact (YYYY-MM-DD)")
    to_period_end: _IsoDate = Field(description="period end of the later fact (YYYY-MM-DD)")


_MODELS: dict[str, type[_Args]] = {
    "lookup_company": LookupCompanyArgs, "search_filings": SearchFilingsArgs, "financial_metrics": FinancialMetricsArgs,
    "risk_changes": RiskChangesArgs, "relationships": RelationshipsArgs, "active_risks": ActiveRisksArgs,
    "compute_change": ComputeChangeArgs}


def _descriptions() -> dict[str, str]:
    return {
        "lookup_company": ("Resolve a company name or ticker. Returns its canonical name, whether it files with the SEC and the fiscal "
                           f"years of annual filings the graph holds. Companies: {', '.join(S.KNOWN_COMPANIES)}. Only these file with "
                           f"the SEC: {', '.join(sorted(S.SEC_FILERS))}."),
        "search_filings": ("Vector search over filing excerpts for a topic the plain retrieval may have missed. New excerpts are added "
                           "to the context; you are told only how many."),
        "financial_metrics": ("Reported annual XBRL metrics (revenue, net_income, rnd, capex) of companies: the latest periods plus any "
                              "named fiscal years or period ends. Adds them to the context and returns the metric names, period "
                              "ends and values."),
        "risk_changes": ("What changed in a company's risk-factor disclosures between annual filings (text-verified comparison of "
                         "removed / new / reworded items and passages), for the latest pair, for named fiscal years or across "
                         "recent reports. Adds it to the context and returns the counts."),
        "relationships": ("Supplier / customer / dependency / competitor links of companies and the Federal Register rules linked to "
                          "them. Adds them to the context and returns counts and names."),
        "active_risks": "The currently active risk disclosures of companies most relevant to a topic. Adds them to the context.",
        "compute_change": ("Percentage change of one metric between two period ends, computed in code from metric facts already "
                           "fetched (call financial_metrics for both periods first). Never do this arithmetic yourself."),
    }


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def tool_specs() -> list[dict]:
    """The OpenAI function-tool schemas of the seven tools, generated from the argument models (titles removed: they only cost
    tokens on every planner call)."""
    texts = _descriptions()
    return [{"type": "function", "function": {"name": name, "description": texts[name],
                                              "parameters": _strip_titles(_MODELS[name].model_json_schema())}}
            for name in TOOL_NAMES]


# --- outcomes ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolOutcome:
    """One tool call: the (possibly new) retrieval dict, what the planner is told, the one-line summary of the step event, and
    whether the call worked. ``tool`` and ``args`` are what is RECORDED of the call (clipped for a refused one)."""

    tool: str
    args: dict
    r: dict
    result: dict
    summary: str
    ok: bool


class ToolError(Exception):
    """A call the tool refuses: ``result`` is what the planner is told (fixed text), ``summary`` the step line."""

    def __init__(self, result: dict, summary: str):
        super().__init__(summary)
        self.result, self.summary = result, summary


def _problems(error: ValidationError) -> str:
    """``field: reason`` for the first few problems: the field NAME (clipped: a refused extra argument names whatever the model
    invented) and pydantic's error type code, never the offending value."""
    parts = [f"{S.clip_name('.'.join(str(p) for p in e['loc'])) if e['loc'] else 'arguments'}: {e['type']}"
             for e in error.errors(include_input=False, include_url=False, include_context=False)[:4]]
    return "; ".join(parts)


def _parse(raw: Any) -> dict | None:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _run_cypher(driver, query: str, *, timeout: float | None = None, **params) -> list[dict]:
    """``retrieval.retriever.run_cypher`` PLUS an optional per-call SERVER-SIDE transaction timeout (M3 finding #3): a
    tool's Cypher queries have no bound of their own otherwise, so one slow or hung query can hold the whole time budget
    (and the answer slot) past it. This is an AGENT-LOCAL wrapper -- the shared ``retrieval.retriever`` module (owned by
    the fixed path too) is never touched, so the fixed path's behaviour cannot change here.

    ``timeout`` becomes ``neo4j.Query(text, timeout=...)``: a server-side transaction timeout in seconds (never a Cypher
    PARAMETER -- ``session.run``'s ``**kwargs`` are query parameters, not driver config). ``None`` (the default, what
    every call outside the agent uses) runs exactly as ``retriever.run_cypher`` always has. ``neo4j.Query(timeout=0)``
    means NO timeout at all (run forever), so a non-positive value refuses the call outright instead."""
    if timeout is not None and timeout <= 0:
        raise TimeoutError("no time remained in the budget for this query")
    q = _CypherQuery(query, timeout=timeout) if timeout is not None else query
    with driver.session() as session:
        return [dict(row) for row in session.run(q, **params)]


@dataclass
class ToolCallBudget:
    """What one tool call may spend, threaded down to every Cypher query the call makes: a shared DEADLINE (a tool that
    runs several queries -- ``risk_changes`` in named mode, ``active_risks`` once per company -- gets ``timeout`` for the
    WHOLE call, not per query, so each successive query gets whatever is left of it, never a fresh 12 seconds) and how
    many more edges the RUN may still add (M3 finding #7: the cap is per run, not per call)."""

    clock: Callable[[], float]
    deadline: float | None                   # an absolute time (``clock()`` units), or None: no timeout for this call
    edges_budget: int = M.MAX_EDGES_ADDED

    def remaining(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - self.clock())


class Toolbox:
    """The tools bound to one driver, one embedder and the user's question (the tools that rank use the question)."""

    def __init__(self, driver, embedder, question: str, *, clock: Callable[[], float] = time.monotonic):
        self.driver, self.embedder, self.question, self._clock = driver, embedder, question, clock
        self._handlers: dict[str, Callable[[Any, dict, ToolCallBudget], ToolOutcome]] = {
            name: getattr(self, f"_{name}") for name in TOOL_NAMES}

    # --- the one entry point -------------------------------------------------------------------------------------------------

    def execute(self, name: Any, raw_arguments: Any, r: dict, *, timeout: float | None = None,
               edges_budget: int = M.MAX_EDGES_ADDED) -> ToolOutcome:
        """Run one tool call. Never raises; ``r`` is never mutated (a call that changes nothing returns ``r`` itself).

        ``timeout`` bounds every Cypher query the call makes TOGETHER (M3 finding #3): ``None`` (the default, what a
        direct call outside the graph loop gets) never touches the fixed path's own queries. ``edges_budget`` caps how
        many more edges ``relationships`` may add ACROSS THE WHOLE RUN, not just this call (M3 finding #7)."""
        tool = S.clip_name(name)
        if not isinstance(name, str) or name not in self._handlers:
            return self._refused(tool, r, {}, {"error": "unknown tool", "tools": list(TOOL_NAMES)}, f"{tool}: refused, unknown tool")
        args = _parse(raw_arguments)
        if args is None:
            return self._refused(tool, r, {}, {"error": "arguments must be a JSON object"}, f"{tool}: refused, arguments are not a JSON object")
        try:
            model = _MODELS[name].model_validate(args)
        except ValidationError as e:
            return self._refused(tool, r, S.clip_args(args), {"error": f"invalid arguments ({_problems(e)})"},
                                 f"{tool}: refused, invalid arguments")
        budget = ToolCallBudget(self._clock, None if timeout is None else self._clock() + timeout, edges_budget)
        try:
            return self._handlers[name](model, r, budget)
        except ToolError as e:
            return self._refused(tool, r, model.model_dump(mode="json", exclude_defaults=True), e.result, e.summary)
        except Exception as e:  # noqa: BLE001 - a tool must never raise into the planner loop; the type name is all that is reported
            logger.warning("tool %s failed: %s", tool, type(e).__name__, exc_info=True)
            return self._refused(tool, r, model.model_dump(mode="json", exclude_defaults=True),
                                 {"error": "the tool failed", "type": type(e).__name__}, f"{tool}: failed ({type(e).__name__})")

    def _cypher(self, query: str, budget: ToolCallBudget, **params) -> list[dict]:
        return _run_cypher(self.driver, query, timeout=budget.remaining(), **params)

    @staticmethod
    def _refused(tool: str, r: dict, args: dict, result: dict, summary: str) -> ToolOutcome:
        return ToolOutcome(tool, args, r, result, summary, False)

    @staticmethod
    def _done(tool: str, model: BaseModel, r: dict, result: dict, summary: str) -> ToolOutcome:
        return ToolOutcome(tool, model.model_dump(mode="json", exclude_defaults=True), r, result, summary, True)

    @staticmethod
    def _resolve(names: list[str]) -> dict[str, int]:
        """Canonical name -> entity id (the graph's ``Company.cik``), in call order, deduplicated."""
        found: dict[str, int] = {}
        for text in names:
            name = S.canonical_name(text)
            if name is None:
                raise ToolError({"error": "unknown company", "known_companies": list(S.KNOWN_COMPANIES)},
                                "refused: a company is not in the known universe")
            found[name] = S.entity_id_of(name)
        return found

    # --- the tools -------------------------------------------------------------------------------------------------------------

    def _lookup_company(self, a: LookupCompanyArgs, r: dict, budget: ToolCallBudget) -> ToolOutcome:
        (name, entity_id), = self._resolve([a.name]).items()
        filer = name in S.SEC_FILERS
        years: list[int] = []
        if filer:
            rows = self._cypher(R.ANNUAL_PAIRS_QUERY, budget, ids=[entity_id])
            years = sorted({y for row in rows for y in (S.safe_year(row.get("older_fy")), S.safe_year(row.get("newer_fy"))) if y})
        return self._done("lookup_company", a, r, {"company": name, "sec_filer": filer, "annual_filing_fiscal_years": years},
                          f"lookup_company: {name} ({'SEC filer' if filer else 'no SEC filings'}, {len(years)} annual filing years)")

    def _search_filings(self, a: SearchFilingsArgs, r: dict, budget: ToolCallBudget) -> ToolOutcome:
        ids = self._resolve(a.companies) if a.companies else {}
        vec = self.embedder.encode_query(a.query)
        if ids:
            rows = self._cypher(R.EXCERPTS_QUERY, budget, ids=list(ids.values()), vec=vec, k=a.k, candidates=R.EXCERPT_CANDIDATES)
        else:
            rows = self._cypher(R.VECTOR_QUERY, budget, k=a.k, vec=vec)
        merged = M.add_anchors(M.merge_chunks(r, rows), ids)
        added = len(merged["chunks"]) - len(r["chunks"])
        return self._done("search_filings", a, merged,
                          {"chunks_returned": len(rows), "chunks_added": added, "chunks_total": len(merged["chunks"]),
                           "chunk_cap": M.MAX_CHUNKS},
                          f"search_filings: {added} new excerpts ({len(merged['chunks'])} in total)")

    def _financial_metrics(self, a: FinancialMetricsArgs, r: dict, budget: ToolCallBudget) -> ToolOutcome:
        ids = self._resolve(a.companies)
        years, dates = sorted(set(a.fiscal_years)), sorted(set(a.period_ends))
        rows = self._cypher(R.METRICS_QUERY, budget, ids=list(ids.values()), periods=R.METRIC_PERIODS_FETCHED, years=years, dates=dates)
        keep = [row for row in rows if not a.metrics or row.get("metric") in a.metrics]
        merged = M.add_anchors(M.merge_metrics(r, keep, years=years, dates=dates), ids)
        added = len(merged["metrics"]) - len(r["metrics"])
        result = {**S.view_metrics(keep), "available_metrics": sorted({m for row in rows if (m := S.safe_metric(row.get("metric")))}),
                  "rows_added": added}
        return self._done("financial_metrics", a, merged, result, f"financial_metrics: {added} new metric rows for {', '.join(ids)}")

    def _risk_changes(self, a: RiskChangesArgs, r: dict, budget: ToolCallBudget) -> ToolOutcome:
        ids = self._resolve(a.companies)
        id_list = list(ids.values())
        mode = "named" if a.fiscal_years else "multi" if a.multi_year else None
        notices: list[dict] = []
        if mode is None:                                       # the current pair, exactly as the plain retrieval reads it
            items, pairs = R.select_temporal(self._cypher(R.TEMPORAL_QUERY, budget, ids=id_list), self.question)
        else:
            periods = {"years": sorted(set(a.fiscal_years)), "dates": []}
            chosen, notices = R.select_pairs(self._cypher(R.ANNUAL_PAIRS_QUERY, budget, ids=id_list), self.question, periods, mode=mode)
            newer = [p["newer_accession"] for p in chosen if p.get("queryable")]
            rows = self._cypher(R.TEMPORAL_SELECTED_QUERY, budget, ids=id_list, newer_accessions=newer) if newer else []
            items, pairs = R.select_temporal(rows, self.question, pairs=chosen)
        comparable = [{"cik": p["cik"], "older": p["older_accession"], "newer": p["newer_accession"]} for p in pairs if p.get("compared", True)]
        passage_rows = self._cypher(R.PASSAGES_QUERY, budget, pairs=comparable) if comparable else []
        passages, pairs = R.select_passages(passage_rows, pairs, self.question)
        merged = M.add_anchors(M.merge_temporal(r, items=items, pairs=pairs, passages=passages, notices=notices,
                                                question=self.question), ids)
        return self._done("risk_changes", a, merged, {"comparisons": S.view_pairs(pairs), "notices": len(notices)},
                          f"risk_changes: {len(pairs)} comparisons for {', '.join(ids)}")

    def _relationships(self, a: RelationshipsArgs, r: dict, budget: ToolCallBudget) -> ToolOutcome:
        ids = self._resolve(a.companies)
        id_list = list(ids.values())
        rows = self._cypher(R.company_edges_query(a.hops), budget, ids=id_list)
        rows += self._cypher(R.RULE_EDGES_QUERY, budget, ids=id_list, include_neighbours=R.NEIGHBOUR_RULES and a.hops >= 2,
                             per_company=R.RULES_PER_COMPANY)
        cap = max(0, min(M.MAX_EDGES_ADDED, budget.edges_budget))       # the RUN's remaining edge budget, not a fresh 40 (finding #7)
        merged = M.add_anchors(M.merge_edges(r, rows, cap=cap), ids)
        added = len(merged["edges"]) - len(r["edges"])
        return self._done("relationships", a, merged, {**S.view_edges(rows), "edges_added": added},
                          f"relationships: {added} new edges for {', '.join(ids)}")

    def _active_risks(self, a: ActiveRisksArgs, r: dict, budget: ToolCallBudget) -> ToolOutcome:
        ids = self._resolve(a.companies)
        vec = self.embedder.encode_query(a.topic or self.question)
        rows: list[dict] = []
        for entity_id in ids.values():
            rows += self._cypher(R.ACTIVE_RISKS_QUERY, budget, cik=entity_id, vec=vec, candidates=R.ACTIVE_RISKS_PER_ANCHOR)
        rows = sorted(rows, key=lambda row: row["score"], reverse=True)[:R.ACTIVE_RISKS_TOP]
        merged = M.add_anchors(M.merge_risks(r, rows), ids)
        added = len(merged["risks"]) - len(r["risks"])
        return self._done("active_risks", a, merged,
                          {"risks_returned": len(rows), "risks_added": added, "risks_total": len(merged["risks"])},
                          f"active_risks: {added} new risks for {', '.join(ids)}")

    def _compute_change(self, a: ComputeChangeArgs, r: dict, budget: ToolCallBudget) -> ToolOutcome:
        # pure: no Cypher query at all, so ``budget`` (timeout / edges) is unused here.
        (name, entity_id), = self._resolve([a.company]).items()
        try:
            merged, info = M.compute_change(r, cik=entity_id, metric=a.metric, from_end=a.from_period_end, to_end=a.to_period_end,
                                            company=name)
        except M.ComputeError as e:
            raise ToolError({"error": str(e)}, "compute_change: refused, the facts cannot support it") from e
        pct = None if info["pct"] is None else round(info["pct"], 1)
        shown = "n/m" if pct is None else f"{pct:+.1f}%"
        return self._done("compute_change", a, merged, {"change_percent": pct, "line": info["line"], "ids": info["ids"]},
                          f"compute_change: {shown} {a.metric} for {name}")

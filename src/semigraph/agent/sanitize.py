"""What the planner model may see: the prompt-injection defence of the agent (docs/v2/M3_AGENT_PLAN.md section 1).

The planner is a cheap model that chooses lookups; it must never be steered by text an attacker could have planted in a filing.
So everything it is shown (the prefetch summary, every tool result, every step summary) is built from an ALLOWLIST of values:

- ids that match the citation grammar (``retrieval.ids``), ISO dates, metric names (``[a-z][a-z0-9_]*``), three-letter currency
  units, numbers as numbers, fiscal years as small integers;
- company names ONLY after mapping through the canonical entity dictionary (a graph node name or an edge endpoint is reduced to the
  canonical name it contains, or dropped and counted as "other"); a raw graph string is never echoed;
- counts.

Nothing else: no chunk text, no risk summary, no headline, no passage text, no quote, no rule title, no ``not_compared_reason``. A
field that fails its check is dropped (or shown as None), never repaired. The tests plant an injection in EVERY free-text field the
graph can return and assert that none of it reaches a planner message.
"""

import json
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from math import isfinite
from typing import Any

from ..artifacts import load_canonical_entities
from ..retrieval import ids as _ids
from ..retrieval.retriever import DEFAULT_ANCHOR_CIK, detect_anchors

MAX_VIEW_ROWS = 40              # metric rows shown to the planner in one result
MAX_PERIODS_PER_METRIC = 6
MAX_RULE_IDS = 8
MAX_ARG_CHARS = 80
MAX_ARG_ITEMS = 10
MAX_ARGS_TOTAL_CHARS = 600      # the TOTAL a call's clipped arguments may serialize to (see clip_args: the per-item bounds
                                # above compose combinatorially -- 10 keys x 10 nested items x 10 list items x 80 chars is
                                # 80,000 characters even though every individual piece is bounded -- so the built structure
                                # is re-clipped against this total before it is ever put in a step event)
MAX_NAME_CHARS = 40
RELATIONS = ("SUPPLIES_TO", "DEPENDS_ON", "CUSTOMER_OF", "COMPETES_WITH", "AFFECTED_BY")
RULE_RELATION = "AFFECTED_BY"
# The graph's only Metric names (ingestion.xbrl.KEY_CONCEPTS' keys): the allowlist is the actual vocabulary, not a
# permissive shape check, so a metric name is never a free-text channel to the planner (M3 finding #8).
KNOWN_METRICS = ("revenue", "net_income", "rnd", "capex")

_METRIC_RE = re.compile(r"^(?:{})\Z".format("|".join(KNOWN_METRICS)))
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_UNIT_RE = re.compile(r"^[A-Za-z]{3}(?:/[A-Za-z]{1,10})?\Z")
_YEAR_RANGE = (1990, 2100)


@lru_cache(maxsize=1)
def _universe() -> tuple[dict[str, str], dict[int, str], tuple[str, ...], frozenset[str]]:
    """(alias or ticker, lower case -> canonical name; entity id or cik -> canonical name; all names; names that file with the SEC)."""
    canonical = load_canonical_entities()
    by_alias: dict[str, str] = {}
    by_id: dict[int, str] = {}
    for name, spec in canonical.items():
        for key in {name, spec.get("ticker") or "", *(spec.get("aliases") or [])}:
            if key:
                by_alias[key.lower()] = name
        for field in ("cik", "entity_id"):
            if spec.get(field) is not None:
                by_id[int(spec[field])] = name
    filers = frozenset(name for name, spec in canonical.items() if spec.get("cik") is not None)
    return by_alias, by_id, tuple(canonical), filers


KNOWN_COMPANIES: tuple[str, ...] = _universe()[2]
SEC_FILERS: frozenset[str] = _universe()[3]


def canonical_name(text: Any) -> str | None:
    """The canonical company a string names: an exact name / alias / ticker (any case), else the ONE company an alias inside it
    names ("Advanced Micro Devices, Inc."). None for anything else, including a string that names several companies. The output
    is always a canonical name, never a piece of the input."""
    if not isinstance(text, str) or not text.strip():
        return None
    exact = _universe()[0].get(text.strip().lower())
    if exact:
        return exact
    found = detect_anchors(text)
    return next(iter(found)) if len(found) == 1 else None


def company_for_id(entity_id: Any) -> str | None:
    """The canonical company of a cik / entity id (the graph's ``Company.cik``); None for an id outside the universe."""
    try:
        return _universe()[1].get(int(entity_id))
    except (TypeError, ValueError):
        return None


def entity_id_of(name: str) -> int | None:
    """The entity id (``Company.cik`` in the graph) of a canonical name."""
    spec = load_canonical_entities().get(name)
    return int(spec["entity_id"]) if spec else None


def safe_metric(value: Any) -> str | None:
    return value if isinstance(value, str) and _METRIC_RE.match(value) else None


def safe_date(value: Any) -> str | None:
    return value if isinstance(value, str) and _DATE_RE.match(value) else None


def safe_unit(value: Any) -> str | None:
    return value if isinstance(value, str) and _UNIT_RE.match(value) else None


def safe_id(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 120 and _ids.classify_id(value) else None


def safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return None
    return int(value) if float(value).is_integer() and abs(value) < 1e15 else value


def safe_year(value: Any) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and _YEAR_RANGE[0] <= value <= _YEAR_RANGE[1]
    return value if ok else None


def safe_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _company_of(row: Mapping) -> str | None:
    return company_for_id(row.get("cik")) or canonical_name(row.get("company"))


# --- views: what a tool result or the prefetch summary is made of ----------------------------------------------------------------

def view_metrics(rows: Iterable[Mapping], cap: int = MAX_VIEW_ROWS) -> dict:
    """``{"companies": [{"company", "metrics": {metric: [{"period_end", "value", "unit"}, ...newest first]}}], "truncated"}``.

    A row whose company, metric name, period end or value fails its check is dropped; a unit that fails is None."""
    grouped: dict[str, dict[str, list[dict]]] = {}
    for row in sorted(rows, key=lambda m: str(m.get("period_end")), reverse=True):
        company, metric = _company_of(row), safe_metric(row.get("metric"))
        end, value = safe_date(row.get("period_end")), safe_number(row.get("value"))
        if not (company and metric and end and value is not None):
            continue
        grouped.setdefault(company, {}).setdefault(metric, []).append({"period_end": end, "value": value, "unit": safe_unit(row.get("unit"))})
    truncated, budget = False, cap
    companies = []
    for company, metrics in grouped.items():
        shown: dict[str, list[dict]] = {}
        for metric, periods in metrics.items():
            keep = periods[:min(MAX_PERIODS_PER_METRIC, budget)]
            truncated |= len(keep) < len(periods)
            if keep:
                shown[metric] = keep
                budget -= len(keep)
        if shown:
            companies.append({"company": company, "metrics": shown})
    return {"companies": companies, "truncated": truncated}


def view_pairs(pairs: Iterable[Mapping]) -> list[dict]:
    """One dict per compared filing pair: company, whether it was compared, the two filing dates and fiscal years, and the
    TOTALS of removed / unsettled / new / reworded items and of removed / added / reworded passages. No headline, no reason."""
    out = []
    for pair in pairs:
        company = _company_of(pair)
        if not company:
            continue
        totals, passages = pair.get("totals") or {}, pair.get("passage_totals") or {}
        out.append({"company": company, "compared": pair.get("compared") is not False,
                    "older_filed": safe_date(pair.get("older_date")), "newer_filed": safe_date(pair.get("newer_date")),
                    "older_fiscal_year": safe_year(pair.get("older_fy")), "newer_fiscal_year": safe_year(pair.get("newer_fy")),
                    "risk_items": {k: safe_count(totals.get(k)) for k in ("removed", "unsettled", "new", "reworded")},
                    "passages": {k: safe_count(passages.get(k)) for k in ("removed", "added", "reworded")}})
    return out


def view_edges(rows: Iterable[Mapping]) -> dict:
    """Counts of edges by relation, the universe companies they connect, how many endpoints were outside the universe, and the
    valid ids of the Federal Register rules. An edge with a relation outside the fixed vocabulary is skipped whole."""
    by_relation: dict[str, int] = {}
    companies: set[str] = set()
    other_companies = other_relations = 0
    rule_ids: list[str] = []
    for edge in rows:
        relation = edge.get("relation")
        if relation not in RELATIONS:
            other_relations += 1
            continue
        by_relation[relation] = by_relation.get(relation, 0) + 1
        if relation == RULE_RELATION:
            rule = safe_id(_ids.fr_id(edge["rule_id"])) if isinstance(edge.get("rule_id"), str) else None
            if rule and rule not in rule_ids and len(rule_ids) < MAX_RULE_IDS:
                rule_ids.append(rule)
            endpoints = [edge.get("source")]
        else:
            endpoints = [edge.get("source"), edge.get("target")]
        for endpoint in endpoints:
            name = canonical_name(endpoint)
            if name:
                companies.add(name)
            else:
                other_companies += 1
    return {"by_relation": by_relation, "companies": sorted(companies), "other_companies": other_companies,
            "other_relations": other_relations, "rule_ids": rule_ids}


def prefetch_summary(r: Mapping) -> dict:
    """What the plain hybrid retrieval already holds, for the planner's first message: the companies it anchored on (or the default
    it fell back to), the metric periods per company, the risk-change comparisons and the counts of every layer."""
    defaulted = bool(r.get("anchor_defaulted"))
    named = sorted({name for key in (r.get("anchors") or {}) if (name := canonical_name(key))})
    metrics: dict[str, dict[str, list[str]]] = {}
    for company in view_metrics(r.get("metrics") or [], cap=10_000)["companies"]:
        metrics[company["company"]] = {metric: [p["period_end"] for p in periods] for metric, periods in company["metrics"].items()}
    edges = r.get("edges") or []
    return {"companies_in_question": [] if defaulted else named,
            "defaulted_to": company_for_id(DEFAULT_ANCHOR_CIK) if defaulted else None,
            "metrics": metrics,
            "risk_comparisons": view_pairs(r.get("temporal_pairs") or []),
            "counts": {"chunks": len(r.get("chunks") or []), "edges": sum(1 for e in edges if e.get("relation") != RULE_RELATION),
                       "rules": sum(1 for e in edges if e.get("relation") == RULE_RELATION), "active_risks": len(r.get("risks") or []),
                       "temporal_items": len(r.get("temporal") or []), "passages": len(r.get("temporal_passages") or [])}}


# --- what is RECORDED (and emitted) about a call the planner made ------------------------------------------------------------------

def clip_name(name: Any) -> str:
    """A tool name the model produced, reduced to something safe to record and emit: word characters only, 40 at most."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name))[:MAX_NAME_CHARS] or "invalid"


def _clip_value(value: Any, depth: int) -> Any:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value[:MAX_ARG_CHARS]
    if depth <= 0:
        return None
    if isinstance(value, (list, tuple)):
        return [_clip_value(v, depth - 1) for v in list(value)[:MAX_ARG_ITEMS]]
    if isinstance(value, Mapping):
        return {str(k)[:MAX_NAME_CHARS]: _clip_value(v, depth - 1) for k, v in list(value.items())[:MAX_ARG_ITEMS]}
    return str(value)[:MAX_ARG_CHARS]


def clip_args(args: Any) -> dict:
    """The arguments of a call the planner made, bounded (a string 80 characters, a list 10 items, two levels deep, the
    WHOLE structure :data:`MAX_ARGS_TOTAL_CHARS`) so a refused call can be recorded and shown without carrying anything
    large. The per-item bounds alone do not compose into a total bound (10 top-level keys, each a dict of 10 keys, each a
    list of 10 eighty-character strings, is 80,000 characters), so the built structure is re-clipped against the total
    afterwards."""
    if not isinstance(args, Mapping):
        return {}
    clipped = {str(k)[:MAX_NAME_CHARS]: _clip_value(v, 2) for k, v in list(args.items())[:MAX_ARG_ITEMS]}
    return _shrink_to_total(clipped, MAX_ARGS_TOTAL_CHARS)


def _shrink_to_total(value: dict, budget: int) -> dict:
    """``value`` (already bounded per item and per depth) with keys dropped or replaced by a placeholder, in order, until
    its JSON form fits ``budget`` characters."""
    if len(json.dumps(value)) <= budget:
        return value
    trimmed: dict = {}
    for k, v in value.items():
        candidate = {**trimmed, k: v}
        if len(json.dumps(candidate)) <= budget:
            trimmed = candidate
            continue
        shortened = {**trimmed, k: "...(clipped)"}
        if len(json.dumps(shortened)) <= budget:
            trimmed = shortened
        break
    return trimmed

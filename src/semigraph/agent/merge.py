"""Pure merges of tool results into the retrieval dict ``r`` (the shape ``retrieval.retriever.hybrid_retrieve`` returns).

Every function returns a NEW dict (lists are rebuilt, nothing is mutated), so the prefetch dict is still intact after any number of
tool calls: that is what makes "the agent degrades to today's behaviour" true (a fallback answers from that untouched prefetch), and
what lets the tests compare a snapshot of the prefetch before and after a tool-heavy run.

Caps (each is a context-size guard): chunks 16 in total, edges 40 ADDED PER RUN (never per call), risks 12 in total, computed
lines 4, filing pairs 5 PER COMPANY in total. None of them ever drops a chunk, edge, risk, computed line or pair the prefetch
already had. The temporal layer's PAIRS and NOTICES are the same: a pair or notice the prefetch already holds is never
dropped, only added to (see :func:`merge_temporal`). Its per-pair ITEM and PASSAGE lines are the one exception: when a company
gains a second pair, the two pairs share the same total line budget one pair always had
(:func:`~semigraph.retrieval.retriever.select_temporal`'s own per-pair caps), so an existing pair's shown lines CAN shrink even
though its pair, and its true (uncapped) totals, never do.

Companies: the context never covers more than ``retriever.MAX_ANCHORS`` of them in all, the prefetch's own counting
first (:func:`add_anchors`, :func:`check_company_cap`). The tools refuse a call that would add a company past it before
they run a query, and it is the company count that bounds the agent's spend estimate (``serve.estimate``).

``compute_change`` is the one place a number is derived. It reads only facts already in ``r["metrics"]`` and writes a
``computed: ...`` line in the grammar ``retrieval.verify`` grounds (``+65.5%`` right after the colon, so the percentage is checked
against its sign), carrying BOTH ``[xbrl:...]`` ids. The ids only count as citable if ``build_blocks`` SHOWS the two rows (it renders
the newest three periods plus the ones the question names), so the two period ends are added to ``r["metric_periods"]``.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date

from ..retrieval import ids as _ids
from ..retrieval import retriever as R
from .sanitize import company_for_id, safe_unit

MAX_CHUNKS = 16
MAX_EDGES_ADDED = 40
MAX_RISKS = 12
MAX_COMPUTED = 4
# Filing pairs of ONE company in the context, over every tool call. A call reads at most ``retriever.MAX_PAIRS_PER_COMPANY``
# (2) of them and the prefetch holds at most as many, so without this the count was bounded only by the graph (at most 4
# consecutive comparisons of any company today). With it the agent's spend estimate is a bound the code enforces:
# ``serve.estimate.AGENT_GRAPH_CHARS_PER_COMPANY`` prices up to 5 (a test renders 5 through the real layout), and 6 do not fit.
MAX_PAIRS_HELD_PER_COMPANY = 5
PERIOD_LENGTH_TOLERANCE_DAYS = 10     # two periods are "the same kind" when their lengths differ by no more than this


class ComputeError(ValueError):
    """A computation the retrieved metrics cannot support. The message is fixed text (safe to show the planner)."""


class CompanyCapError(ValueError):
    """Adding the companies ``adding`` would take the context past ``cap`` companies; nothing was added. ``covered`` are
    the names the context already holds. Both lists are sorted names, ``room`` is how many more companies still fit."""

    def __init__(self, covered: Iterable[str], adding: Iterable[str], cap: int):
        self.covered, self.adding, self.cap = sorted(covered), sorted(adding), cap
        super().__init__(f"{len(self.covered)} companies covered, {len(self.adding)} would be added: the cap is {cap}")

    @property
    def room(self) -> int:
        return max(0, self.cap - len(self.covered))


def company_cap() -> int:
    """The most companies one answer's context may carry: the retriever's anchor cap, read when asked (never copied)
    so the plain retrieval, the agent and the spend estimate (``serve.estimate``) cannot drift apart."""
    return R.MAX_ANCHORS


def covered_companies(r: Mapping) -> dict[int, str]:
    """``{entity id: name}`` of the companies whose blocks ``r`` carries: its anchors, plus the default company the
    retrieval fell back to when it detected none (``anchors`` is then empty, yet that company's rules, metrics and
    temporal blocks are in the context)."""
    covered = {entity_id: name for name, entity_id in (r.get("anchors") or {}).items()}
    if r.get("anchor_defaulted"):
        covered.setdefault(R.DEFAULT_ANCHOR_CIK, company_for_id(R.DEFAULT_ANCHOR_CIK) or "the default company")
    return covered


def check_company_cap(r: Mapping, anchors: Mapping[str, int], *, cap: int | None = None) -> None:
    """Raise :class:`CompanyCapError` when adding ``anchors`` to ``r`` would take the companies it covers past ``cap``
    (:func:`company_cap` by default). Companies are counted by entity id: one ``r`` already covers is not an addition,
    and a call that adds none never raises (a prefetch over a lowered cap still answers)."""
    limit = company_cap() if cap is None else cap
    covered = covered_companies(r)
    new = {entity_id: name for name, entity_id in anchors.items() if entity_id not in covered}
    if new and len(covered) + len(new) > limit:
        raise CompanyCapError(covered.values(), new.values(), limit)


def _uncover_dropped(r: dict, anchors: Mapping[str, int]) -> dict:
    """``r`` once the companies in ``anchors`` are covered: they leave ``anchors_dropped`` and the "not covered" note
    that names them (rewritten for the ones still dropped, removed with the key when none are), so the writer is never
    told a company is not covered when its blocks are in the context. The note is found by equality with the retriever's
    own, never by its look: a notice a tool wrote is not touched."""
    dropped = list(r.get("anchors_dropped") or [])
    remaining = [name for name in dropped if name not in anchors]
    if len(remaining) == len(dropped):
        return r
    old_note = R._dropped_notice(dropped)
    notices: list[dict] = []
    for note in r.get("temporal_notices") or []:
        if note != old_note:
            notices.append(note)
        elif remaining:
            notices.append(R._dropped_notice(remaining))          # in place: the note keeps its position in the block
    out = {**r, "temporal_notices": notices}
    if remaining:
        out["anchors_dropped"] = remaining
    else:
        del out["anchors_dropped"]
    return out


def add_anchors(r: dict, anchors: Mapping[str, int], *, cap: int | None = None) -> dict:
    """``r`` with ``anchors`` added to ``r["anchors"]``. ``anchor_defaulted`` is the PREFETCH's fact and is left alone.

    Raises :class:`CompanyCapError` (and adds nothing) when that would take the context past ``cap`` companies
    (:func:`company_cap` by default): never a clamp, which would leave ``anchors`` saying less than the rows already
    merged. The tools check first (:func:`check_company_cap`), before they run a query, so this is the backstop. A
    company the retrieval dropped and a tool legitimately adds is no longer "not covered" (:func:`_uncover_dropped`)."""
    check_company_cap(r, anchors, cap=cap)
    return _uncover_dropped({**r, "anchors": {**(r.get("anchors") or {}), **anchors}}, anchors)


def merge_chunks(r: dict, rows: Iterable[Mapping], *, cap: int = MAX_CHUNKS) -> dict:
    """The excerpt rows appended after the existing chunks, deduplicated by ``chunk_id``, up to ``cap`` chunks in total."""
    have = {c["chunk_id"] for c in r["chunks"]}
    room = max(0, cap - len(r["chunks"]))
    added: list[dict] = []
    for row in rows:
        chunk_id = row.get("chunk_id")
        if not chunk_id or chunk_id in have or len(added) >= room:
            continue
        have.add(chunk_id)
        added.append(dict(row))
    return {**r, "chunks": [*r["chunks"], *added]}


def _union(existing: Iterable, more: Iterable) -> list:
    return sorted({*existing, *more})


def show_periods(r: dict, *, years: Iterable[int] = (), dates: Iterable[str] = ()) -> dict:
    """``r`` with fiscal ``years`` / period-end ``dates`` added to the periods the METRICS block must show."""
    periods = r.get("metric_periods") or {}
    return {**r, "metric_periods": {"years": _union(periods.get("years") or [], years),
                                    "dates": _union(periods.get("dates") or [], dates)}}


def merge_metrics(r: dict, rows: Iterable[Mapping], *, years: Iterable[int] = (), dates: Iterable[str] = ()) -> dict:
    """A deduplicated union of the metric rows (by cik, metric, period end); ``years`` / ``dates`` are the periods the call was
    about, so the block shows them (see :func:`show_periods`)."""
    seen = {(m.get("cik"), m.get("metric"), m.get("period_end")) for m in r["metrics"]}
    added = []
    for row in rows:
        key = (row.get("cik"), row.get("metric"), row.get("period_end"))
        if key not in seen:
            seen.add(key)
            added.append(dict(row))
    return show_periods({**r, "metrics": [*r["metrics"], *added]}, years=years, dates=dates)


def _not_of(rows: Iterable[Mapping], ciks: set) -> list:
    return [row for row in rows if row.get("cik") not in ciks]


def _pair_key(row: Mapping) -> tuple:
    return row.get("cik"), row.get("newer_accession")


def _pairs_past_the_cap(r: Mapping, pairs: list[dict], cap: int) -> list[dict]:
    """The pairs of this call that a company has no room for, ``cap`` pairs of one company in all. The pairs ``r`` already
    holds come first (never dropped, and a repeat of one is a refresh, not an addition), then the call's NEW pairs newest
    first, however the call lists them. A company already at or over ``cap`` (a lowered cap) takes none."""
    held = r.get("temporal_pairs") or []
    held_keys = {_pair_key(p) for p in held}
    count = Counter(p.get("cik") for p in held)
    refused: list[dict] = []
    new_newest_first = sorted((p for p in pairs if _pair_key(p) not in held_keys), key=lambda p: p.get("newer_date") or "",
                              reverse=True)
    for pair in new_newest_first:
        if count[pair.get("cik")] < cap:
            count[pair.get("cik")] += 1
        else:
            refused.append(pair)
    return refused


def _pair_cap_notices(refused: list[dict], cap: int) -> list[dict]:
    """One note per company that lost a pair to the cap, worded without a count of the lost ones, so the same note from a
    later call is the same note (:func:`merge_temporal` keeps one copy of a note)."""
    text = f"the context holds {cap} annual-filing comparisons, the most it carries for one company; further ones were not added"
    companies = {p.get("cik"): p.get("company") for p in refused}
    return [{"cik": cik, "company": company, "text": text} for cik, company in companies.items()]


def merge_temporal(r: dict, *, items: list[dict], pairs: list[dict], passages: list[dict], notices: list[dict],
                   question: str = "", pair_cap: int = MAX_PAIRS_HELD_PER_COMPANY) -> dict:
    """The temporal layers of the touched companies UNIONED with what the prefetch already had, keyed by
    ``(cik, newer_accession)``: a second call about the exact SAME pair refreshes it (its old items, passages and notice
    belong to that one question and are replaced), but a call that names a DIFFERENT pair of a company the prefetch
    already covers (an older fiscal year, while the prefetch holds the current one) ADDS it alongside -- it never drops
    the company's other pairs, unlike a plain per-company replace (M3 finding #4: the package's own "only ADDS to the
    prefetch" claim was not true of this layer before this fix). Every other company's layers are untouched.

    Per-pair item / passage caps are RECOMPUTED across a touched company's unioned pairs with the retriever's own
    :func:`~semigraph.retrieval.retriever.select_temporal` / :func:`~semigraph.retrieval.retriever.select_passages`
    (never reinvented here) -- exactly the machinery ``hybrid_retrieve`` itself uses when a question names several pairs,
    so a company with two pairs shares the same total line budget it always would with one. That sharing CAN shrink how
    many lines an individual pair shows (two pairs split one cap): the merged layers are a superset of the prefetch's
    PAIRS and NOTICES, never of every one of its item/passage LINES. Each pair's ``totals`` / ``passage_totals`` (the
    TRUE uncapped counts, "showing 4 of 8") are restored from whichever side owns that pair afterwards, since
    ``select_temporal`` / ``select_passages`` would otherwise recompute them from the already-capped rows fed back in.

    A company holds at most ``pair_cap`` pairs in all (:data:`MAX_PAIRS_HELD_PER_COMPANY`), so the context a company adds
    to the prompt is bounded by the code and not by what the graph happens to hold (the spend estimate rests on it). The
    pairs the context already holds are never dropped for it; of a call's NEW pairs the newest are kept, and the items and
    passages of a pair that did not fit are not merged. A pair that did not fit is a CLAMP, not a refusal like the company
    cap (:func:`add_anchors`): the extra comparison is optional detail, while a clamped anchor would leave ``anchors``
    saying less than the rows merged. It is not silent: the company's note says the context holds the most it carries.

    Nothing is replaced when the call returned no pair and no notice."""
    refused = _pairs_past_the_cap(r, pairs, pair_cap)
    if refused:
        dropped = {_pair_key(p) for p in refused}
        pairs = [p for p in pairs if _pair_key(p) not in dropped]
        items = [row for row in items if _pair_key(row) not in dropped]
        passages = [row for row in passages if _pair_key(row) not in dropped]
        notices = [*notices, *_pair_cap_notices(refused, pair_cap)]
    refreshed = {_pair_key(p) for p in pairs}
    ciks = {p.get("cik") for p in pairs} | {n.get("cik") for n in notices}
    kept_pairs = [p for p in (r.get("temporal_pairs") or []) if p.get("cik") in ciks and _pair_key(p) not in refreshed]
    kept_items = [it for it in (r.get("temporal") or []) if it.get("cik") in ciks and _pair_key(it) not in refreshed]
    kept_passages = [p for p in (r.get("temporal_passages") or []) if p.get("cik") in ciks and _pair_key(p) not in refreshed]
    union_pairs = [*kept_pairs, *pairs]
    totals_by_key = {_pair_key(p): (p.get("totals"), p.get("passage_totals")) for p in union_pairs}

    new_items, recapped = R.select_temporal([*kept_items, *items], question, pairs=union_pairs)
    new_passages, recapped = R.select_passages([*kept_passages, *passages], recapped, question)

    fixed_pairs = []
    for p in recapped:
        totals, passage_totals = totals_by_key.get(_pair_key(p), (None, None))
        fixed = dict(p)
        if totals is not None:
            fixed["totals"] = totals
        if passage_totals is not None:
            fixed["passage_totals"] = passage_totals
        fixed_pairs.append(fixed)

    notice_keys = {(n.get("cik"), n.get("text")) for n in notices}
    kept_notices = [n for n in (r.get("temporal_notices") or []) if (n.get("cik"), n.get("text")) not in notice_keys]

    return {**r,
            "temporal": [*_not_of(r.get("temporal") or [], ciks), *new_items],
            "temporal_pairs": [*_not_of(r.get("temporal_pairs") or [], ciks), *fixed_pairs],
            "temporal_passages": [*_not_of(r.get("temporal_passages") or [], ciks), *new_passages],
            "temporal_notices": [*kept_notices, *notices]}


def _edge_key(edge: Mapping) -> tuple:
    return edge.get("source"), edge.get("relation"), edge.get("target"), edge.get("rule_id")


def merge_edges(r: dict, rows: Iterable[Mapping], *, cap: int = MAX_EDGES_ADDED) -> dict:
    """The edge rows not already present, at most ``cap`` of them ADDED (the prefetch's edges are never trimmed)."""
    seen = {_edge_key(e) for e in r["edges"]}
    added: list[dict] = []
    for row in rows:
        key = _edge_key(row)
        if key in seen or len(added) >= cap:
            continue
        seen.add(key)
        added.append(dict(row))
    return {**r, "edges": [*r["edges"], *added]}


def merge_risks(r: dict, rows: Iterable[Mapping], *, cap: int = MAX_RISKS) -> dict:
    """The active-risk rows not already present (by ``chunk_id``), appended after the prefetch's, up to ``cap`` in total."""
    have = {k["chunk_id"] for k in r["risks"]}
    room = max(0, cap - len(r["risks"]))
    added: list[dict] = []
    for row in rows:
        if row.get("chunk_id") in have or len(added) >= room:
            continue
        have.add(row["chunk_id"])
        added.append(dict(row))
    return {**r, "risks": [*r["risks"], *added]}


# --- compute_change -----------------------------------------------------------------------------------------------------------

def _fact(r: dict, cik: int, metric: str, period_end: str) -> dict | None:
    for m in r.get("metrics") or []:
        try:
            same_company = int(m.get("cik")) == int(cik)
        except (TypeError, ValueError):
            continue
        if same_company and m.get("metric") == metric and m.get("period_end") == period_end:
            return m
    return None


def _span_days(row: Mapping) -> int | None:
    """Length of a reporting period in days; None for an instant (a balance-sheet fact has no start)."""
    try:
        return (date.fromisoformat(row["period_end"]) - date.fromisoformat(row["period_start"])).days
    except (KeyError, TypeError, ValueError):
        return None


def _same_kind(older: Mapping, newer: Mapping) -> bool:
    a, b = _span_days(older), _span_days(newer)
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= PERIOD_LENGTH_TOLERANCE_DAYS


# Two period ENDS this close together are year-over-year (matches retrieval.answerer.yoy_note's own window); further apart
# (or closer, an edge case no real annual pair produces) the line says how many fiscal years it spans instead, so a
# multi-year change can never be mistaken for "year over year" (M3 finding #5). Whole fiscal years, not the period's own
# length (``_same_kind`` above): two adjacent 52-week fiscal years and a three-year span are both "annual" periods.
ADJACENT_PERIOD_GAP_DAYS = (350, 380)


def _period_gap_years(older: Mapping, newer: Mapping) -> tuple[int | None, bool]:
    """``(whole fiscal years between the two period ends, is that gap "adjacent" i.e. year-over-year)``; ``(None, False)``
    when the gap cannot be read."""
    try:
        gap_days = (date.fromisoformat(newer["period_end"]) - date.fromisoformat(older["period_end"])).days
    except (KeyError, TypeError, ValueError):
        return None, False
    adjacent = ADJACENT_PERIOD_GAP_DAYS[0] <= gap_days <= ADJACENT_PERIOD_GAP_DAYS[1]
    return round(gap_days / 365.25), adjacent


def _period_span(older: Mapping, newer: Mapping, citations: list[str]) -> str:
    """The ``... from ... to ...`` clause of a computed line: the ORIGINAL wording (unchanged, byte for byte) for a
    year-over-year gap; ``over N fiscal years`` for a wider one; the original wording again for anything in between (a
    gap under a year, or one that rounds to a single fiscal year without being adjacent: never claim a year count that
    could read as "year over year" when it is not)."""
    years, adjacent = _period_gap_years(older, newer)
    plain = f"from period ended {older['period_end']} [{citations[0]}] to period ended {newer['period_end']} [{citations[1]}]"
    if adjacent or years is None or years < 2:
        return plain
    return (f"over {years} fiscal years, from fiscal year ended {older['period_end']} [{citations[0]}] to fiscal year "
            f"ended {newer['period_end']} [{citations[1]}]")


def _computed_line(company: str, metric: str, older: Mapping, newer: Mapping, citations: list[str]) -> tuple[str, float | None]:
    unit = older.get("unit") or "USD"
    before, now = float(older["value"]), float(newer["value"])
    span = _period_span(older, newer, citations)
    change = f"{now - before:+,.0f} {unit}"
    if before <= 0:
        return (f"computed: n/m change in {metric} for {company} {span} (change {change}; percentage not meaningful, "
                "prior value not positive)", None)
    pct = (now - before) / before * 100
    return f"computed: {pct:+.1f}% change in {metric} for {company} {span} (change {change})", pct


# Precedes the run's computed lines the first time one is added, so several computed lines from different calls do not
# visually sit under the wrong company's METRICS header (M3 finding #9). Not itself a computed line: never counted
# against MAX_COMPUTED, added at most once per run (compute_change re-derives it fresh on every call, filtering any
# earlier copy out first, so it can never appear twice however many times compute_change runs in one plan).
COMPUTED_HEADER = "Computed changes:"


def compute_change(r: dict, *, cik: int, metric: str, from_end: str, to_end: str, company: str | None = None) -> tuple[dict, dict]:
    """``(new r, info)``: the change of ``metric`` between two periods, computed in code from facts ALREADY in ``r["metrics"]``.

    ``info`` is ``{"line", "ids", "pct", "from", "to"}`` (``pct`` None when the base is not positive). Raises :class:`ComputeError`
    when a fact is not among the retrieved metrics (fetch it with ``financial_metrics`` first), the periods are not in order, the
    units differ or the two periods are not the same kind of period (an annual against a quarterly figure). ``r["computed"]``
    always starts with :data:`COMPUTED_HEADER` once any line has been added."""
    older, newer = _fact(r, cik, metric, from_end), _fact(r, cik, metric, to_end)
    if older is None or newer is None:
        raise ComputeError("a fact is not among the retrieved metrics: fetch it with financial_metrics first")
    if not from_end < to_end:
        raise ComputeError("from_period_end must be earlier than to_period_end")
    units = {older.get("unit") or "USD", newer.get("unit") or "USD"}
    if len(units) != 1:
        raise ComputeError("the two facts have different units")
    if safe_unit(next(iter(units))) is None:
        raise ComputeError("the facts have no valid currency unit")
    if not _same_kind(older, newer):
        raise ComputeError("the two facts cover periods of different length")
    citations = [_ids.xbrl_id(older["cik"], metric, from_end), _ids.xbrl_id(newer["cik"], metric, to_end)]
    if not all(_ids.XBRL_ID_RE.match(c) for c in citations):
        raise ComputeError("the facts cannot form citation ids")
    existing = [line for line in (r.get("computed") or []) if line != COMPUTED_HEADER]
    line, pct = _computed_line(company or newer.get("company") or "the company", metric, older, newer, citations)
    if line not in existing and len(existing) >= MAX_COMPUTED:
        raise ComputeError("too many computed changes")
    info = {"line": line, "ids": citations, "pct": pct, "from": dict(older), "to": dict(newer)}
    lines = existing if line in existing else [*existing, line]
    out = {**r, "computed": [COMPUTED_HEADER, *lines]}
    return show_periods(out, dates=[from_end, to_end]), info

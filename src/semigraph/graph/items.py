"""The ``align-items`` pipeline (M1b step 4): text-grounded alignment of risk items between consecutive annual filings.

Replaces the LLM-summary lineage clustering (which reported risks as "Deleted" although their text was still in the newer
filing). For every ticker and every consecutive annual pair of filings that have risk items (``graph/item_pairs``):

* a COMPARABLE pair (both sides pass the quality sidecar of ``parsing/risk_items``) is aligned with ``alignment.align`` (lexical
  only, no embeddings) against the FULL newer section text, optionally settled by the LLM adjudication (``graph/adjudicate``, off by
  default), and decomposed into changed passages by ``passages.compute_passages`` with the chunk spans of both filings;
* a NON-comparable pair is recorded with ``comparable = false`` and the reason and produces NOTHING else: no decision, no
  passage (the graph loader stores ``items_compared = false`` on the filing pair and every consumer says "comparison not available").

Output (per ticker, ``data/interim/risk_alignment/``, written atomically, byte-identical on a re-run of the same inputs):

* ``<T>_pairs.parquet``: pair_id, ticker, older_accession, newer_accession, older_date, newer_date, comparable, not_compared_reason;
* ``<T>_decisions.parquet``: one row per item and side of a comparable pair (an item appears once as ``newer`` in the pair that
  brought it in and once as ``older`` in the pair that follows): item_id, accession_no, side, label, matched_item_id, decided_by,
  sim_embed, sim_lex, headline_ratio, quote, quote_span_start, quote_span_end, adjudicated, pair_id;
* ``<T>_passages.parquet``: every ``Passage`` field (``counterpart_span`` as counterpart_start / counterpart_end, ``chunk_ids`` as a
  list) plus older_accession, newer_accession, filer_cik, counterpart_chunk_ids (the chunks of the OTHER filing that overlap the
  counterpart span), chunk_fallback (True when the passage overlaps no chunk of its own and carries its item's FIRST chunk id
  instead), pair_id.

Contract facts (M1B_PLAN L.7): a REMOVED item is an older item labelled ``removed`` (after adjudication) in a comparable pair;
``uncertain`` is PRESENT, never removed; ``is_new`` is a newer item labelled ``new``; a ``merged`` older item is present.
"""

import logging
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..config import Settings
from ..parsing.risk_item_quality import load_quality
from . import adjudicate as adj
from .alignment import AlignmentResult, AlignParams, align, summarize
from .item_pairs import consecutive_pairs, risk_section_text
from .loaders import _chunks_path, _section_texts_path
from .passages import Passage, PassageParams, compute_passages

logger = logging.getLogger("semigraph.graph.items")

ALIGNMENT_DIRNAME = "risk_alignment"
ITEMS_DIRNAME = "risk_items"
DEFAULT_MAX_USD = 0.5
_S, _I, _F, _B = pa.string(), pa.int64(), pa.float64(), pa.bool_()

PAIR_SCHEMA = pa.schema([
    ("pair_id", _S), ("ticker", _S), ("older_accession", _S), ("newer_accession", _S), ("older_date", _S),
    ("newer_date", _S), ("comparable", _B), ("not_compared_reason", _S)])
DECISION_SCHEMA = pa.schema([
    ("item_id", _S), ("accession_no", _S), ("side", _S), ("label", _S), ("matched_item_id", _S), ("decided_by", _S),
    ("sim_embed", _F), ("sim_lex", _F), ("headline_ratio", _F), ("quote", _S), ("quote_span_start", _I),
    ("quote_span_end", _I), ("adjudicated", _B), ("pair_id", _S)])
PASSAGE_SCHEMA = pa.schema([
    ("passage_id", _S), ("kind", _S), ("item_id", _S), ("seq", _I), ("text", _S), ("char_start", _I), ("char_end", _I),
    ("counterpart_text", _S), ("counterpart_start", _I), ("counterpart_end", _I), ("similarity", _F),
    ("chunk_ids", pa.list_(_S)), ("decided_by", _S), ("older_accession", _S), ("newer_accession", _S), ("filer_cik", _I),
    ("counterpart_chunk_ids", pa.list_(_S)), ("chunk_fallback", _B), ("pair_id", _S)])
TABLES = {"pairs": PAIR_SCHEMA, "decisions": DECISION_SCHEMA, "passages": PASSAGE_SCHEMA}


class AlignItemsError(RuntimeError):
    """The inputs of the pipeline are missing or inconsistent (the message says what to run)."""


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def alignment_dir(settings: Settings) -> Path:
    return settings.interim_dir / ALIGNMENT_DIRNAME


def items_dir(settings: Settings) -> Path:
    return settings.interim_dir / ITEMS_DIRNAME


def table_path(directory: Path, ticker: str, table: str) -> Path:
    return directory / f"{ticker}_{table}.parquet"


def discover_tickers(settings: Settings, requested: Sequence[str] | None = None) -> list[str]:
    """The requested tickers (validated against the item files) or every ticker that has a risk-item parquet, sorted."""
    have = sorted(p.name.split("_")[0] for p in items_dir(settings).glob("*_risk_items.parquet"))
    if not requested:
        return have
    wanted = [t.upper() for t in requested]
    missing = [t for t in wanted if t not in have]
    if missing:
        raise AlignItemsError(f"no risk-item parquet for {missing} in {items_dir(settings)}: run `semigraph risk-items` first")
    return sorted(set(wanted))


def missing_alignment(settings: Settings, tickers: Sequence[str], directory: Path | None = None) -> list[Path]:
    """The alignment parquets the loader needs that do not exist yet (in ``directory``, default the lake's alignment dir)."""
    directory = directory or alignment_dir(settings)
    return [p for t in tickers for name in TABLES if not (p := table_path(directory, t, name)).exists()]


def check_alignment_consistent(ticker: str, items: pd.DataFrame, pairs: pd.DataFrame, decisions: pd.DataFrame,
                               passages: pd.DataFrame) -> None:
    """The alignment tables must describe THESE items (an item file rebuilt after `align-items` leaves them stale)."""
    known = set(items["item_id"])
    stale = (set(decisions["item_id"]) | set(passages["item_id"])) - known
    accessions = set(items["accession_no"])
    stale_pairs = {a for a in set(pairs["older_accession"]) | set(pairs["newer_accession"]) if a not in accessions}
    if stale or stale_pairs:
        raise AlignItemsError(f"{ticker}: the alignment files do not match the risk-item file (unknown items {sorted(stale)[:3]}, "
                              f"unknown filings {sorted(stale_pairs)[:3]}): run `semigraph align-items` first")
    compared = set(pairs.loc[pairs["comparable"], "pair_id"])
    outside = (set(decisions["pair_id"]) | set(passages["pair_id"])) - compared
    if outside:
        raise AlignItemsError(f"{ticker}: decisions or passages exist for pair(s) not compared {sorted(outside)[:3]}: "
                              "run `semigraph align-items` first")


def require_alignment(settings: Settings, tickers: Sequence[str], directory: Path | None = None) -> None:
    """Raise ``AlignItemsError`` ("run semigraph align-items first") when any ticker lacks its alignment files or has files that
    no longer describe its risk items. Cheap (ids only): `build-graph` calls it before anything is reset."""
    if not tickers:
        raise AlignItemsError(f"no risk items in {items_dir(settings)}: run `semigraph risk-items` and `semigraph align-items` first")
    directory = directory or alignment_dir(settings)
    missing = missing_alignment(settings, tickers, directory)
    if missing:
        raise AlignItemsError(f"no risk-item alignment for {sorted({p.name.split('_')[0] for p in missing})} in "
                              f"{directory}: run `semigraph align-items` first")
    for ticker in tickers:
        check_alignment_consistent(
            ticker, pd.read_parquet(items_dir(settings) / f"{ticker}_risk_items.parquet", columns=["item_id", "accession_no"]),
            pd.read_parquet(table_path(directory, ticker, "pairs")),
            pd.read_parquet(table_path(directory, ticker, "decisions"), columns=["item_id", "pair_id"]),
            pd.read_parquet(table_path(directory, ticker, "passages"), columns=["item_id", "pair_id"]))


# --------------------------------------------------------------------------
# rows from the lake (pure helpers)
# --------------------------------------------------------------------------

def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""          # None / NaN -> ""


def item_rows(items: pd.DataFrame, accession: str) -> list[dict]:
    """The items of one filing in filing order (``char_start``, then id) as plain dict rows for ``align`` / ``compute_passages``."""
    rows = items[items["accession_no"] == accession].sort_values(["char_start", "item_id"], kind="stable")
    return [{"item_id": r.item_id, "headline": _str(r.headline), "text": r.text, "text_hash": r.text_hash,
             "unit_kind": r.unit_kind, "char_start": int(r.char_start), "char_end": int(r.char_end),
             "section_id": r.section_id, "seq": int(r.seq), "filer_cik": int(r.filer_cik),
             "chunk_ids": [str(c) for c in r.chunk_ids]}
            for r in rows.itertuples(index=False)]


def section_chunk_spans(chunks: pd.DataFrame, accession: str, section_id: str) -> list[tuple[str, int, int]]:
    """``(chunk_id, char_start, char_end)`` of the chunks of ONE section of one filing (offsets are section-text offsets)."""
    sel = chunks[(chunks["accession_no"] == accession) & (chunks["section_id"] == section_id)]
    return [(str(c), int(a), int(b)) for c, a, b in zip(sel["chunk_id"], sel["char_start"], sel["char_end"])]


def overlapping_chunk_ids(spans: Sequence[tuple[str, int, int]], start: int, end: int) -> tuple[str, ...]:
    """Chunk ids whose half-open range overlaps ``[start, end)`` (zero-length chunks ignored), in text order."""
    return tuple(cid for cid, a, b in sorted(spans, key=lambda s: (s[1], s[2], s[0])) if b > a and a < end and b > start)


# --------------------------------------------------------------------------
# aligning one pair
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PairWork:
    """A pair with everything needed to align it. ``result`` is None for a pair that is not compared."""

    pair: dict
    older_rows: list[dict] = field(default_factory=list)
    newer_rows: list[dict] = field(default_factory=list)
    older_text: str = ""
    newer_text: str = ""
    older_spans: list[tuple[str, int, int]] = field(default_factory=list)
    newer_spans: list[tuple[str, int, int]] = field(default_factory=list)
    result: AlignmentResult | None = None


@dataclass(frozen=True)
class PairOutcome:
    """A pair after adjudication and passages. ``before`` is the aligner's own result (to tell what adjudication changed)."""

    work: PairWork
    result: AlignmentResult | None
    before: AlignmentResult | None
    passages: tuple[Passage, ...] = ()
    adjudicated: frozenset[str] = frozenset()


def prepare_pair(pair: dict, items: pd.DataFrame, sections: pd.DataFrame, chunks: pd.DataFrame,
                 params: AlignParams = AlignParams()) -> PairWork:
    """Align one pair (lexical only, no embedding), or return it as not compared."""
    if not pair["comparable"]:
        return PairWork(pair)
    o_acc, n_acc = pair["older_accession"], pair["newer_accession"]
    older, newer = item_rows(items, o_acc), item_rows(items, n_acc)
    older_text, newer_text = risk_section_text(items, sections, o_acc), risk_section_text(items, sections, n_acc)
    result = align(older, newer, newer_text, params=params, older_section_text=older_text)
    return PairWork(pair, older, newer, older_text, newer_text,
                    section_chunk_spans(chunks, o_acc, older[0]["section_id"]) if older else [],
                    section_chunk_spans(chunks, n_acc, newer[0]["section_id"]) if newer else [], result)


def finalize_pair(work: PairWork, records: Mapping[str, Mapping[str, Any]] | None = None, *, with_passages: bool = True,
                  adj_params: adj.AdjudicationParams = adj.AdjudicationParams(),
                  passage_params: PassageParams = PassageParams()) -> PairOutcome:
    """Apply the recorded model answers (``records``: older item id -> ``{"verdict", "quote"}``) and compute the passages."""
    if work.result is None:
        return PairOutcome(work, None, None)
    result, adjudicated = work.result, frozenset()
    if records:
        settled = adj.settle(work.result, work.older_rows, work.newer_rows, work.newer_text, records, adj_params)
        result, adjudicated = settled.result, settled.adjudicated
    passages = (compute_passages(work.older_rows, work.newer_rows, result, work.older_text, work.newer_text,
                                 work.older_spans, work.newer_spans, passage_params) if with_passages else ())
    return PairOutcome(work, result, work.result, passages, adjudicated)


# --------------------------------------------------------------------------
# rows for the parquet tables
# --------------------------------------------------------------------------

def pair_rows(outcomes: Sequence[PairOutcome]) -> list[dict]:
    keys = [f.name for f in PAIR_SCHEMA]
    return [{k: o.work.pair.get(k) for k in keys} for o in outcomes]


def _decision_row(pair_id: str, accession: str, side: str, d: Any, adjudicated: bool) -> dict:
    ev = d.evidence
    matched = d.matched_newer_id if side == "older" else d.matched_older_id
    return {"item_id": d.item_id, "accession_no": accession, "side": side, "label": d.label, "matched_item_id": matched,
            "decided_by": d.decided_by, "sim_embed": ev.embed_sim, "sim_lex": ev.lex_sim, "headline_ratio": ev.headline_ratio,
            "quote": ev.quote, "quote_span_start": ev.quote_span[0] if ev.quote_span else None,
            "quote_span_end": ev.quote_span[1] if ev.quote_span else None, "adjudicated": adjudicated, "pair_id": pair_id}


def decision_rows(outcome: PairOutcome) -> list[dict]:
    """Older decisions then newer decisions, in item order. ``adjudicated``: the item got a model verdict (older side) or its
    decision was changed by one (newer side)."""
    if outcome.result is None:
        return []
    pair = outcome.work.pair
    rows = [_decision_row(pair["pair_id"], pair["older_accession"], "older", d, d.item_id in outcome.adjudicated)
            for d in outcome.result.older]
    before = {n.item_id: n for n in outcome.before.newer}
    rows += [_decision_row(pair["pair_id"], pair["newer_accession"], "newer", n, n != before[n.item_id])
             for n in outcome.result.newer]
    return rows


def passage_rows(outcome: PairOutcome) -> list[dict]:
    """One row per passage with the chunk ids of its own filing, the counterpart's chunk ids and the recorded fallback."""
    if not outcome.passages:
        return []
    work, pair = outcome.work, outcome.work.pair
    item_chunks = {r["item_id"]: r["chunk_ids"] for r in work.older_rows + work.newer_rows}
    cik = (work.older_rows or work.newer_rows)[0]["filer_cik"]
    rows = []
    for p in outcome.passages:
        own = list(p.chunk_ids)
        fallback = not own and bool(item_chunks.get(p.item_id))
        if fallback:
            own = [item_chunks[p.item_id][0]]
        counterpart = (list(overlapping_chunk_ids(work.newer_spans, *p.counterpart_span)) if p.counterpart_span else [])
        rows.append({
            "passage_id": p.passage_id, "kind": p.kind, "item_id": p.item_id, "seq": p.seq, "text": p.text,
            "char_start": p.char_start, "char_end": p.char_end, "counterpart_text": p.counterpart_text,
            "counterpart_start": p.counterpart_span[0] if p.counterpart_span else None,
            "counterpart_end": p.counterpart_span[1] if p.counterpart_span else None, "similarity": p.similarity,
            "chunk_ids": own, "decided_by": p.decided_by, "older_accession": pair["older_accession"],
            "newer_accession": pair["newer_accession"], "filer_cik": cik, "counterpart_chunk_ids": counterpart,
            "chunk_fallback": fallback, "pair_id": pair["pair_id"]})
    return rows


def summary_row(outcome: PairOutcome, *, passages_computed: bool = True) -> dict:
    """Counts for the per-pair table (``passages_*`` are None when the passages were not computed: a dry run)."""
    pair = outcome.work.pair
    base = {"pair_id": pair["pair_id"], "ticker": pair["ticker"], "older_date": pair["older_date"],
            "newer_date": pair["newer_date"], "compared": bool(pair["comparable"]),
            "not_compared_reason": pair["not_compared_reason"]}
    if outcome.result is None:
        return base
    s = summarize(outcome.result)
    kinds = Counter(p.kind for p in outcome.passages)
    return {**base, "older_items": s["older"]["total"], "newer_items": s["newer"]["total"],
            **{f"older_{k}": s["older"][k] for k in ("unchanged", "reworded", "merged", "removed", "uncertain")},
            **{f"newer_{k}": s["newer"][k] for k in ("carried", "new", "uncertain")},
            **{f"passages_{k}": (kinds[k] if passages_computed else None) for k in ("removed", "reworded", "added")},
            "adjudicated": len(outcome.adjudicated)}


# --------------------------------------------------------------------------
# writing (atomic, deterministic)
# --------------------------------------------------------------------------

def write_table(path: Path, rows: Sequence[Mapping[str, Any]], schema: pa.Schema) -> None:
    """Write ``rows`` with a fixed schema through a temp file and ``os.replace``: no torn file, and the same rows always give the
    same bytes (fixed schema and column order, no index, no timestamps, one compression)."""
    table = pa.Table.from_pylist([dict(r) for r in rows], schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        pq.write_table(table, tmp, compression="snappy")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def check_unique_passages(ticker: str, rows: Sequence[Mapping[str, Any]]) -> None:
    dup = [k for k, n in Counter(r["passage_id"] for r in rows).items() if n > 1]
    if dup:
        raise AlignItemsError(f"{ticker}: duplicate passage_id(s) {dup[:5]}: the graph's passage_id constraint would reject them")


def write_ticker(directory: Path, ticker: str, outcomes: Sequence[PairOutcome]) -> list[Path]:
    """Write the three tables of one ticker (rows sorted by pair, then item / passage order) and return the paths."""
    ordered = sorted(outcomes, key=lambda o: o.work.pair["pair_id"])
    passages = [r for o in ordered for r in passage_rows(o)]
    check_unique_passages(ticker, passages)
    payload = {"pairs": pair_rows(ordered), "decisions": [r for o in ordered for r in decision_rows(o)], "passages": passages}
    paths = []
    for name, rows in payload.items():
        path = table_path(directory, ticker, name)
        write_table(path, rows, TABLES[name])
        paths.append(path)
    return paths


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AlignRun:
    """What ``run_align_items`` did. ``estimate`` is None unless adjudication was requested; ``written`` is empty for a dry run."""

    summary: list[dict]
    estimate: adj.Estimate | None
    written: list[Path]
    dry_run: bool


def read_ticker_inputs(settings: Settings, ticker: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """``(items, section texts, chunk spans)`` of one ticker from the lake."""
    path = items_dir(settings) / f"{ticker}_risk_items.parquet"
    sections_path, chunks_path = _section_texts_path(settings, ticker), _chunks_path(settings, ticker)
    for needed in (path, sections_path, chunks_path):
        if not needed.exists():
            raise AlignItemsError(f"{needed} not found: run `semigraph ingest` and `semigraph risk-items` first")
    chunks = pd.read_parquet(chunks_path, columns=["accession_no", "section_id", "chunk_id", "char_start", "char_end"])
    return pd.read_parquet(path), pd.read_parquet(sections_path), chunks


def _records_for(work: PairWork, tasks: Mapping[str, adj.Task], checkpoint: adj.Checkpoint | None) -> dict[str, dict]:
    """The recorded model answers of this pair's tasks, keyed by older item id."""
    if checkpoint is None:
        return {}
    return {t.item_id: checkpoint.records[t.key] for t in tasks.get(work.pair["pair_id"], []) if t.key in checkpoint.records}


def run_align_items(settings: Settings, tickers: Sequence[str] | None = None, *, adjudicate: bool = False,
                    max_usd: float = DEFAULT_MAX_USD, dry_run: bool = False,
                    llm: Callable[..., Any] | None = None, model: str | None = None, out_dir: Path | None = None,
                    align_params: AlignParams = AlignParams(),
                    adj_params: adj.AdjudicationParams = adj.AdjudicationParams()) -> AlignRun:
    """Align every consecutive annual pair of every ticker and write the three tables per ticker.

    Phase 1 (free, pure) aligns every pair. With ``adjudicate``, phase 2 collects the model tasks of the ``uncertain`` / ``removed``
    older items, prints nothing, checks the worst case against ``max_usd`` BEFORE any call (raising ``adjudicate.BudgetExceeded``),
    and answers the tasks the checkpoint does not hold. ``dry_run`` stops after the estimate: nothing is written, no model is
    called, and passages are not computed. Phase 3 settles, computes passages and writes. ``out_dir`` (default: the lake's
    ``risk_alignment`` directory) redirects the tables AND the adjudication checkpoint (tests use it to leave the real files alone)."""
    directory = out_dir or alignment_dir(settings)
    tickers = discover_tickers(settings, tickers)
    if not tickers:
        raise AlignItemsError(f"no risk items in {items_dir(settings)}: run `semigraph risk-items` first")
    quality = load_quality(items_dir(settings))
    per_ticker: dict[str, list[PairWork]] = {}
    for ticker in tickers:
        items, sections, chunks = read_ticker_inputs(settings, ticker)
        per_ticker[ticker] = [prepare_pair(p, items, sections, chunks, align_params)
                              for p in consecutive_pairs(items, quality)]
        logger.info("%s: %d pair(s) aligned", ticker, len(per_ticker[ticker]))
    model = model or settings.adjudication_model
    tasks: dict[str, list[adj.Task]] = {}
    checkpoint = estimate = None
    if adjudicate:
        checkpoint = adj.Checkpoint(directory / adj.CHECKPOINT_NAME)
        for works in per_ticker.values():
            for w in works:
                if w.result is not None:
                    tasks[w.pair["pair_id"]] = adj.plan_tasks(w.pair["pair_id"], w.result, w.older_rows, w.newer_text, model, adj_params)
        estimate = adj.estimate_cost([t for ts in tasks.values() for t in ts], set(checkpoint.records), model, adj_params)
        if not dry_run:
            if estimate.worst_case_usd > max_usd:
                raise adj.BudgetExceeded(f"worst case ${estimate.worst_case_usd:.4f} for {estimate.n_calls} call(s) exceeds "
                                         f"--max-usd ${max_usd:.2f}: nothing was spent")
            adj.run_tasks([t for ts in tasks.values() for t in ts], checkpoint, llm or adj.default_llm_json,
                          model=model, max_usd=max_usd, params=adj_params)
    written: list[Path] = []
    summary: list[dict] = []
    for ticker, works in per_ticker.items():
        outcomes = [finalize_pair(w, _records_for(w, tasks, checkpoint), with_passages=not dry_run, adj_params=adj_params)
                    for w in works]
        summary += [summary_row(o, passages_computed=not dry_run) for o in outcomes]
        if not dry_run:
            written += write_ticker(directory, ticker, outcomes)
    return AlignRun(summary, estimate, written, dry_run)


# --------------------------------------------------------------------------
# the printed table
# --------------------------------------------------------------------------

_COLS = (("pair", "pair_id", 46, "<"), ("older", "older_items", 5, ">"), ("newer", "newer_items", 5, ">"),
         ("unch", "older_unchanged", 5, ">"), ("reword", "older_reworded", 6, ">"), ("merged", "older_merged", 6, ">"),
         ("REMOVED", "older_removed", 7, ">"), ("uncert", "older_uncertain", 6, ">"), ("NEW", "newer_new", 4, ">"),
         ("n-unc", "newer_uncertain", 5, ">"), ("p-rem", "passages_removed", 5, ">"), ("p-rew", "passages_reworded", 5, ">"),
         ("p-add", "passages_added", 5, ">"), ("adj", "adjudicated", 4, ">"))


def format_summary(rows: Sequence[Mapping[str, Any]]) -> str:
    """The per-pair table: item labels, passages per kind, and the reason of every pair that was not compared."""
    lines = ["  ".join(f"{h:{a}{w}}" for h, _, w, a in _COLS)]
    totals: Counter[str] = Counter()
    for r in rows:
        if not r["compared"]:
            lines.append(f"{r['pair_id']:<46}  NOT COMPARED: {r['not_compared_reason']}")
            totals["not_compared"] += 1
            continue
        lines.append("  ".join(f"{r[k]:<{w}}" if k == "pair_id" else f"{'-' if r.get(k) is None else r[k]:{a}{w}}"
                               for _, k, w, a in _COLS))
        totals.update({k: v for k, v in r.items() if isinstance(v, int) and not isinstance(v, bool)})
    lines.append("-" * len(lines[0]))
    computed = any(r.get("passages_removed") is not None for r in rows if r["compared"])
    passages = (f"{totals['passages_removed']} removed / {totals['passages_reworded']} reworded / {totals['passages_added']} added"
                if computed else "not computed (dry run)")
    lines.append(f"{len([r for r in rows if r['compared']])} pair(s) compared, {totals['not_compared']} not compared; "
                 f"removed items {totals['older_removed']}, new items {totals['newer_new']}, passages {passages}")
    return "\n".join(lines)

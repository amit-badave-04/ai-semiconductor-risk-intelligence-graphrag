"""The ``align-items`` pipeline (M1b step 4): text-grounded alignment of risk items between consecutive annual filings.

Replaces the LLM-summary lineage clustering (which reported risks as "Deleted" although their text was still in the newer
filing). For every ticker and every consecutive annual pair of filings that have risk items (``graph/item_pairs``):

* a COMPARABLE pair (both sides pass the quality sidecar of ``parsing/risk_items``) is aligned with ``alignment.align`` (lexical
  only, no embeddings) against the FULL newer section text, optionally settled by the LLM adjudication (``graph/adjudicate``, off by
  default), and decomposed into changed passages by ``passages.PairPassages`` with the chunk spans of both filings; sentences in the
  lexical BAND of the passage layer are settled by cached verdicts of ``graph/passage_adjudicate`` (see below);
* a NON-comparable pair is recorded with ``comparable = false`` and the reason and produces NOTHING else: no decision, no
  passage (the graph loader stores ``items_compared = false`` on the filing pair and every consumer says "comparison not available").

Output (per ticker, ``data/interim/risk_alignment/``, written atomically, byte-identical on a re-run of the same inputs):

* ``<T>_pairs.parquet``: pair_id, ticker, older_accession, newer_accession, older_date, newer_date, comparable, not_compared_reason;
* ``<T>_decisions.parquet``: one row per item and side of a comparable pair (an item appears once as ``newer`` in the pair that
  brought it in and once as ``older`` in the pair that follows): item_id, accession_no, side, label, matched_item_id, decided_by,
  sim_embed, sim_lex, headline_ratio, quote, quote_span_start, quote_span_end, adjudicated, pair_id;
* ``<T>_passages.parquet``: every ``Passage`` field (``counterpart_span`` as counterpart_start / counterpart_end, ``chunk_ids`` as a
  list, ``band_adjudicated``: a model verdict settled at least one sentence) plus older_accession, newer_accession, filer_cik,
  counterpart_chunk_ids (the chunks of the OTHER filing that overlap the counterpart span), chunk_fallback (True when the passage
  overlaps no chunk of its own and carries its item's FIRST chunk id instead), pair_id. (``band_adjudicated`` is the LAST column; the
  graph loader does not read it: the same fact is in ``decided_by``, ``sentence_absent_llm`` / ``sentence_reworded_llm``.)

REPLAY versus BUYING. Every run REPLAYS, for free and deterministically, every checkpoint that exists next to the tables, whatever its
flags: the item-level answers (``adjudications.jsonl``, prompt ``adj-v1``) of the current model, and the passage answers
(``passage_adjudications.jsonl``) of the current model under the NEWEST prompt version present (``pas-v3``, else the legacy ``pas-v2``),
band AND below zone (a recorded below answer switches the zone on for the replay). A replay never calls a model, never embeds and never
spends; a sentence or item without a recorded answer stays as the lexical rules left it (band sentences ``reworded``, marked
``sentence_reworded_band``; aligner-only labels). The flags only permit BUYING the answers that are missing, under ``max_usd``:
``adjudicate=True`` (``--adjudicate``) asks the cheap model about the items the aligner could not settle; ``adjudicate_passages=True``
(``--adjudicate-passages``) about the band sentences (after the item step, on the SETTLED result, because settling can change which items
are decomposed), and with ``passage_params.adjudicate_below_band`` (``--adjudicate-all-absent``) about the sentences with NO counterpart
at all too (zone ``below``: a verified ``same`` turns a wrongly confident removal into a reworded passage, a ``different`` keeps it). The
passage candidates are the lexical best, the ``partial_ratio`` best and the nearest sentences by embedding (``embed``,
``graph/sentence_embed``; the per-section vectors are cached in ``<out dir>/sentence_embeddings/``); a buying run REQUIRES ``embed`` (a dry
run embeds nothing and prices stand-in candidates) and records ``pas-v3``. With ``--max-usd 0`` a fully cached buying run is a free
check: it refuses if anything is uncached. With both flags ``max_usd`` is one budget: the passage step gets what the item step's
per-call upper bounds charged left (a dry run: what its worst-case estimate left). Each run that writes tables also writes
``alignment_provenance.json`` (``graph/alignment_provenance``): ``require_alignment`` (so ``build-graph``) refuses tables that were
built before the checkpoints reached their present state.

Contract facts (M1B_PLAN L.7): a REMOVED item is an older item labelled ``removed`` (after adjudication) in a comparable pair;
``uncertain`` is PRESENT, never removed; ``is_new`` is a newer item labelled ``new``; a ``merged`` older item is present.
"""

import logging
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..config import Settings
from ..parsing.risk_item_quality import load_quality, quality_path_for
from . import adjudicate as adj
from . import alignment_provenance as provenance
from . import passage_adjudicate as pad
from . import sentence_embed
from .alignment import AlignmentResult, AlignParams, align, summarize
from .item_pairs import consecutive_pairs, risk_section_text
from .loaders import _chunks_path, _section_texts_path
from .passages import BandSentence, PairPassages, Passage, PassageParams, compute_passages

logger = logging.getLogger("semigraph.graph.items")

ALIGNMENT_DIRNAME = "risk_alignment"
ITEMS_DIRNAME = "risk_items"
EMBEDDINGS_DIRNAME = "sentence_embeddings"
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
    ("counterpart_chunk_ids", pa.list_(_S)), ("chunk_fallback", _B), ("pair_id", _S), ("band_adjudicated", _B)])
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


def check_provenance(directory: Path, tickers: Sequence[str]) -> None:
    """Refuse tables whose provenance is weaker than the checkpoints now on disk: a checkpoint has answers (or a state) the tables were
    not built with, or the tables carry no provenance while a checkpoint exists (``graph/alignment_provenance``)."""
    stale = provenance.stale_checkpoints(directory, tickers)
    if stale:
        files = sorted({name for names in stale.values() for name in names})
        raise AlignItemsError(f"the alignment tables of {sorted(stale)} are weaker than the recorded model answers in {directory} "
                              f"({', '.join(files)} changed since the tables were built, or the tables carry no provenance): run "
                              "`semigraph align-items` (free: it replays every recorded answer and calls no model), then build the graph again")


def require_alignment(settings: Settings, tickers: Sequence[str], directory: Path | None = None) -> None:
    """Raise ``AlignItemsError`` ("run semigraph align-items first") when any ticker lacks its alignment files, has files that
    no longer describe its risk items, or has tables built before the checkpoints reached their present state
    (:func:`check_provenance`). Cheap (ids and file digests): `build-graph` calls it before anything is reset."""
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
    check_provenance(directory, tickers)


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
    band: int | None = None              # band sentences of the pair (None: not enumerated)
    band_answered: int | None = None     # ... of which a recorded model answer exists
    band_applied: int = 0                # ... which the code rules turned into a verdict (band AND below sentences)
    below: int | None = None             # below sentences of the pair (None: the below zone is off)
    below_answered: int | None = None    # ... of which a recorded model answer exists


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


@dataclass(frozen=True)
class BandContext:
    """The recorded passage answers to replay (``records``: checkpoint key -> raw answer) for ``model``, the rules' parameters and
    ONE prompt version (``run_align_items`` picks the newest present: ``pad.replay_version``; the default is the legacy ``pas-v2``)."""

    records: Mapping[str, Mapping[str, Any]]
    model: str
    params: pad.PassageAdjudicationParams = pad.PassageAdjudicationParams()
    version: str = pad.LEGACY_PROMPT_VERSION


class PairPlan(NamedTuple):
    """The model tasks of one pair and, per zone (``band`` / ``below``), ``(sentences, of which already answered)``."""

    tasks: list[pad.Task]
    counts: dict[str, tuple[int, int]]


def zone_counts(bands: Sequence[BandSentence], is_answered: Callable[[BandSentence], bool]) -> dict[str, tuple[int, int]]:
    """``{zone: (sentences, answered)}`` for every zone (a zone without sentences is ``(0, 0)``)."""
    counts = {zone: [0, 0] for zone in pad.ZONES}
    for band in bands:
        counts[band.zone][0] += 1
        counts[band.zone][1] += bool(is_answered(band))
    return {zone: (n, answered) for zone, (n, answered) in counts.items()}


def settle_pair(work: PairWork, records: Mapping[str, Mapping[str, Any]] | None = None,
                adj_params: adj.AdjudicationParams = adj.AdjudicationParams()) -> tuple[AlignmentResult | None, frozenset[str]]:
    """The pair's result after the recorded item-level answers (``records``: older item id -> ``{"verdict", "quote"}``), and which
    older items had an answer."""
    if work.result is None:
        return None, frozenset()
    if not records:
        return work.result, frozenset()
    settled = adj.settle(work.result, work.older_rows, work.newer_rows, work.newer_text, records, adj_params)
    return settled.result, settled.adjudicated


def finalize_pair(work: PairWork, records: Mapping[str, Mapping[str, Any]] | None = None, *, with_passages: bool = True,
                  adj_params: adj.AdjudicationParams = adj.AdjudicationParams(),
                  passage_params: PassageParams = PassageParams(), band: BandContext | None = None) -> PairOutcome:
    """Apply the recorded item-level answers and compute the passages; with ``band``, the band sentences of the settled result are
    listed and settled by the recorded passage answers (an unanswered band sentence stays ``reworded``)."""
    result, adjudicated = settle_pair(work, records, adj_params)
    if result is None:
        return PairOutcome(work, None, None)
    if not with_passages:
        return PairOutcome(work, result, work.result, (), adjudicated)
    if band is None:
        passages = compute_passages(work.older_rows, work.newer_rows, result, work.older_text, work.newer_text,
                                    work.older_spans, work.newer_spans, passage_params)
        return PairOutcome(work, result, work.result, passages, adjudicated)
    engine = PairPassages(work.older_rows, work.newer_rows, result, work.older_text, work.newer_text,
                          work.older_spans, work.newer_spans, passage_params)
    bands = engine.band_sentences(candidates=False)          # replay needs keys and hashes, not the candidate search
    resolved = pad.resolve_verdicts(bands, band.records, band.model, older_text=work.older_text, newer_text=work.newer_text,
                                    params=band.params, prompt_version=band.version)
    unanswered = set(resolved.unanswered)
    counts = zone_counts(bands, lambda b: b.key not in unanswered)
    below = counts["below"] if passage_params.adjudicate_below_band else (None, None)
    return PairOutcome(work, result, work.result, engine.passages(resolved.verdicts), adjudicated, counts["band"][0],
                       counts["band"][1], len(resolved.verdicts), *below)


def plan_band_tasks(work: PairWork, records: Mapping[str, Mapping[str, Any]] | None, model: str,
                    cached: Mapping[str, Any], *, adj_params: adj.AdjudicationParams = adj.AdjudicationParams(),
                    passage_params: PassageParams = PassageParams(),
                    pas_params: pad.PassageAdjudicationParams = pad.PassageAdjudicationParams(),
                    embed: sentence_embed.EmbedFn | None = None, cache_dir: Path | None = None, dry_run: bool = False,
                    refuse_uncached: bool = False) -> PairPlan:
    """The model tasks of one pair (band and, with ``adjudicate_below_band``, below sentences) on its SETTLED result, and the
    per-zone counts of sentences and of those already answered in ``cached``.

    Cheap first: the sentences are enumerated without candidates and only the ones not answered yet get them (the lexical ones, then
    the embedding neighbours of ``embed``: the sections' sentence vectors are read from ``cache_dir`` or computed, only here). A dry
    run embeds nothing: the candidates not known yet are priced at stand-ins (``pad.with_proxy_candidates``). ``refuse_uncached``:
    raise ``BudgetExceeded`` at the first unanswered sentence, before anything is embedded (there is no budget to buy it)."""
    result, _ = settle_pair(work, records, adj_params)
    if result is None:
        return PairPlan([], {})
    pair_id = work.pair["pair_id"]
    engine = PairPassages(work.older_rows, work.newer_rows, result, work.older_text, work.newer_text, params=passage_params)
    bands = engine.band_sentences(candidates=False)
    todo = {b.key for b in bands if pad.task_key(b.text_hash, b.other_hash, model) not in cached}
    counts = zone_counts(bands, lambda b: b.key not in todo)
    if todo and refuse_uncached:
        raise pad.BudgetExceeded(f"{pair_id}: passage answers are missing and the worst case of buying them exceeds --max-usd: "
                                 "no budget is left for this step (nothing was embedded or spent)")
    fresh, saving = engine.with_candidates([b for b in bands if b.key in todo]), None
    if todo and dry_run:
        fresh, saving = pad.with_proxy_candidates(fresh, work.older_text, work.newer_text, pas_params,
                                                  min_chars=passage_params.min_sentence_chars, max_chars=passage_params.max_candidate_chars)
    elif todo and embed is not None:
        near = sentence_embed.PairNeighbours((work.pair["older_accession"], work.older_text), (work.pair["newer_accession"], work.newer_text),
                                             embed, cache_dir, min_chars=passage_params.min_sentence_chars)
        fresh = pad.with_semantic_candidates(fresh, near.neighbours, work.older_text, work.newer_text, pas_params,
                                             max_chars=passage_params.max_candidate_chars)
    replaced = {b.key: b for b in fresh}
    return PairPlan(pad.plan_tasks(pair_id, [replaced.get(b.key, b) for b in bands], model, pas_params, likely_saving=saving), counts)


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
            "chunk_fallback": fallback, "pair_id": pair["pair_id"], "band_adjudicated": p.band_adjudicated})
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
            "adjudicated": len(outcome.adjudicated), "band": outcome.band, "band_answered": outcome.band_answered,
            "below": outcome.below, "below_answered": outcome.below_answered}


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
    """What ``run_align_items`` did. ``estimate`` is None unless ``adjudicate``; ``written`` is empty for a dry run (else it lists the
    tables and the provenance sidecar). ``passage_estimate`` / ``passage_budget_usd``: the passage step's estimate (the zones added up:
    the guard covers the sum) and the budget it was checked against (None unless ``adjudicate_passages``); ``passage_estimates``: the
    same per zone (``band`` and, with the below zone on, ``below``); ``passage_verdicts_used``: band / below verdicts applied (bought now
    or replayed); ``passage_calls``: model calls MADE by the passage step (``passage_failed`` of them failed: no answer recorded);
    ``passage_prompt_version``: the version whose answers were replayed ('' when none is recorded for the model);
    ``item_verdicts_used``: item-level answers applied (bought now or replayed); ``item_calls`` / ``item_failed`` / ``item_charged_usd``:
    the item step's calls made, of which failed, and the USD of upper bounds it was charged; ``below_replayed``: the below zone was
    replayed; ``provenance``: the sidecar written (None for a dry run)."""

    summary: list[dict]
    estimate: adj.Estimate | None
    written: list[Path]
    dry_run: bool
    passage_estimate: adj.Estimate | None = None
    passage_budget_usd: float | None = None
    passage_verdicts_used: int = 0
    passage_calls: int = 0
    passage_estimates: dict[str, adj.Estimate] = field(default_factory=dict)
    passage_prompt_version: str = ""
    passage_failed: int = 0
    item_verdicts_used: int = 0
    item_calls: int = 0
    item_failed: int = 0
    item_charged_usd: float = 0.0
    below_replayed: bool = False
    provenance: Path | None = None


def ticker_input_paths(settings: Settings, ticker: str) -> dict[str, Path]:
    """The lake files the tables of one ticker are computed from (their digests go into the provenance)."""
    return {"risk_items": items_dir(settings) / f"{ticker}_risk_items.parquet", "section_texts": _section_texts_path(settings, ticker),
            "chunks": _chunks_path(settings, ticker), "quality": quality_path_for(items_dir(settings), ticker)}


def read_ticker_inputs(settings: Settings, ticker: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """``(items, section texts, chunk spans)`` of one ticker from the lake."""
    paths = ticker_input_paths(settings, ticker)
    for needed in (paths["risk_items"], paths["section_texts"], paths["chunks"]):
        if not needed.exists():
            raise AlignItemsError(f"{needed} not found: run `semigraph ingest` and `semigraph risk-items` first")
    chunks = pd.read_parquet(paths["chunks"], columns=["accession_no", "section_id", "chunk_id", "char_start", "char_end"])
    return pd.read_parquet(paths["risk_items"]), pd.read_parquet(paths["section_texts"]), chunks


def item_records(per_ticker: Mapping[str, Sequence[PairWork]], checkpoint: adj.Checkpoint, model: str) -> dict[str, dict[str, dict]]:
    """The recorded item-level answers of every compared pair, ``{pair id: {older item id: raw answer}}`` (pairs without one are
    left out). Only keys are computed (no prompt, no candidate search), so a replay is cheap."""
    out: dict[str, dict[str, dict]] = {}
    for works in per_ticker.values():
        for w in works:
            if w.result is None:
                continue
            keys = adj.task_keys(w.result, w.older_rows, w.newer_text, model)
            found = {item_id: checkpoint.records[key] for item_id, key in keys.items() if key in checkpoint.records}
            if found:
                out[w.pair["pair_id"]] = found
    return out


_NO_CALLS = adj.CallStats(0, 0, 0.0)


class ItemStep(NamedTuple):
    """What the item step planned (``estimate``, None when it was not asked to buy) and did (``stats``)."""

    estimate: adj.Estimate | None
    stats: adj.CallStats


def _buy_item_answers(per_ticker: Mapping[str, Sequence[PairWork]], checkpoint: adj.Checkpoint, model: str, call: Callable[..., Any], *,
                      adj_params: adj.AdjudicationParams, max_usd: float, dry_run: bool) -> ItemStep:
    """The item step of a buying run: plan a task per uncertain / removed older item, estimate, refuse when the worst case is over
    ``max_usd`` BEFORE any call (``BudgetExceeded``), and answer what the checkpoint lacks (skipped: a dry run)."""
    tasks = [t for works in per_ticker.values() for w in works if w.result is not None
             for t in adj.plan_tasks(w.pair["pair_id"], w.result, w.older_rows, w.newer_text, model, adj_params)]
    estimate = adj.estimate_cost(tasks, set(checkpoint.records), model, adj_params)
    if dry_run:
        return ItemStep(estimate, _NO_CALLS)
    if estimate.worst_case_usd > max_usd:
        raise adj.BudgetExceeded(f"worst case ${estimate.worst_case_usd:.4f} for {estimate.n_calls} call(s) exceeds "
                                 f"--max-usd ${max_usd:.2f}: nothing was spent")
    return ItemStep(estimate, adj.run_tasks(tasks, checkpoint, call, model=model, max_usd=max_usd, params=adj_params))


class PassageStep(NamedTuple):
    """What the passage step planned: the total ``estimate``, the same ``by_zone``, the ``stats`` of the calls it made and, per pair
    id, the ``counts`` of sentences and answered ones per zone."""

    estimate: adj.Estimate
    by_zone: dict[str, adj.Estimate]
    stats: adj.CallStats
    counts: dict[str, dict[str, tuple[int, int]]]


def _settle_passages(per_ticker: Mapping[str, Sequence[PairWork]], recorded: Mapping[str, Mapping[str, Any]],
                     passage_cp: adj.Checkpoint, model: str, *, max_usd: float, budget: float,
                     dry_run: bool, call: Callable[..., Any], adj_params: adj.AdjudicationParams, passage_params: PassageParams,
                     pas_params: pad.PassageAdjudicationParams, embed: sentence_embed.EmbedFn | None = None,
                     cache_dir: Path | None = None) -> PassageStep:
    """The passage step: list the band (and below) sentences of every compared pair on its settled result, build the candidates of the
    ones not answered yet, estimate per zone, check ``budget`` against the SUM BEFORE any call (``BudgetExceeded``), and answer what
    the checkpoint lacks, the band zone before the below zone.
    Pairs are planned one at a time and their engines dropped, so the memory of a lake-wide run stays that of one pair. With nothing
    budgeted (``budget <= 0``) the first unanswered sentence refuses the run before any embedding (``--max-usd 0`` replays for free)."""
    planned: list[pad.Task] = []
    counts: dict[str, dict[str, tuple[int, int]]] = {}
    refuse = not dry_run and budget <= 0
    for works in per_ticker.values():
        for w in works:
            if w.result is None:
                continue
            plan = plan_band_tasks(w, recorded.get(w.pair["pair_id"]), model, passage_cp.records, adj_params=adj_params,
                                   passage_params=passage_params, pas_params=pas_params, embed=embed, cache_dir=cache_dir,
                                   dry_run=dry_run, refuse_uncached=refuse)
            planned += plan.tasks
            counts[w.pair["pair_id"]] = plan.counts
    unique = sorted({t.key: t for t in planned}.values(), key=lambda t: pad.ZONES.index(t.zone))       # stable: band first
    cached = set(passage_cp.records)
    estimate = pad.estimate_cost(unique, cached, model, pas_params)
    zones = pad.ZONES if passage_params.adjudicate_below_band else pad.ZONES[:1]
    by_zone = pad.estimates_by_zone(unique, cached, model, zones, pas_params)
    if dry_run:
        return PassageStep(estimate, by_zone, _NO_CALLS, counts)
    if estimate.worst_case_usd > budget:
        raise pad.BudgetExceeded(f"worst case ${estimate.worst_case_usd:.4f} for {estimate.n_calls} passage call(s) exceeds --max-usd "
                                 f"${max_usd:.2f} (${budget:.4f} left after the item step): nothing further was spent")
    return PassageStep(estimate, by_zone, pad.run_tasks(unique, passage_cp, call, model=model, max_usd=budget, params=pas_params),
                       counts)


def _align_all(settings: Settings, tickers: Sequence[str], align_params: AlignParams) -> dict[str, list[PairWork]]:
    """Phase 1 (free, pure): align every consecutive pair of every ticker."""
    quality = load_quality(items_dir(settings))
    per_ticker: dict[str, list[PairWork]] = {}
    for ticker in tickers:
        frame, sections, chunks = read_ticker_inputs(settings, ticker)
        per_ticker[ticker] = [prepare_pair(p, frame, sections, chunks, align_params) for p in consecutive_pairs(frame, quality)]
        logger.info("%s: %d pair(s) aligned", ticker, len(per_ticker[ticker]))
    return per_ticker


def replay_context(passage_cp: adj.Checkpoint, model: str, pas_params: pad.PassageAdjudicationParams,
                   passage_params: PassageParams) -> tuple[BandContext | None, PassageParams]:
    """What the tables replay of the passage checkpoint: the answers of the newest prompt version present for ``model`` (None: none
    is recorded, the tables are the lexical rules' alone) and the passage parameters to compute them with, whose below zone is on
    when the caller asked for it OR that version holds a below answer (an unanswered below sentence is classified as with the zone
    off, so turning it on cannot change a passage that has no answer)."""
    version = pad.replay_version(passage_cp.records, model)
    if version is None:
        return None, passage_params
    below = passage_params.adjudicate_below_band or pad.replay_has_below(passage_cp.records, model, version)
    return (BandContext(passage_cp.records, model, pas_params, version),
            replace(passage_params, adjudicate_below_band=True) if below else passage_params)


@dataclass(frozen=True)
class _Purchases:
    """What the buying steps did before the tables are written (``passage`` is None unless ``adjudicate_passages``)."""

    item: ItemStep
    passage: PassageStep | None
    passage_budget: float | None


def _purchases(per_ticker: Mapping[str, Sequence[PairWork]], directory: Path, model: str, call: Callable[..., Any], *, adjudicate: bool,
               adjudicate_passages: bool, max_usd: float, dry_run: bool, adj_params: adj.AdjudicationParams,
               passage_params: PassageParams, pas_params: pad.PassageAdjudicationParams,
               embed: sentence_embed.EmbedFn | None) -> _Purchases:
    """Phases 2 and 3: BUY the answers that are missing, only for the steps the flags permit (nothing without a flag). The item step
    runs first; the passage step plans on the result settled with EVERY recorded item answer and gets what ``max_usd`` has left after
    the item step's charges (a dry run: after its worst-case estimate)."""
    item = ItemStep(None, _NO_CALLS)
    if adjudicate:
        item = _buy_item_answers(per_ticker, adj.Checkpoint(directory / adj.CHECKPOINT_NAME), model, call, adj_params=adj_params,
                                 max_usd=max_usd, dry_run=dry_run)
    if not adjudicate_passages:
        return _Purchases(item, None, None)
    budget = max(max_usd - (item.estimate.worst_case_usd if dry_run and item.estimate else item.stats.charged), 0.0)
    recorded = item_records(per_ticker, adj.Checkpoint(directory / adj.CHECKPOINT_NAME), model)
    step = _settle_passages(per_ticker, recorded, adj.Checkpoint(directory / pad.CHECKPOINT_NAME), model, max_usd=max_usd,
                            budget=budget, dry_run=dry_run, call=call, adj_params=adj_params, passage_params=passage_params,
                            pas_params=pas_params, embed=embed, cache_dir=directory / EMBEDDINGS_DIRNAME)
    return _Purchases(item, step, budget)


def _with_counts(outcome: PairOutcome, counts: Mapping[str, tuple[int, int]], params: PassageParams) -> PairOutcome:
    """A dry-run outcome carrying the zone counts the passage step enumerated (the below columns only with the below zone on)."""
    below = counts["below"] if params.adjudicate_below_band else (None, None)
    return replace(outcome, band=counts["band"][0], band_answered=counts["band"][1], below=below[0], below_answered=below[1])


def run_align_items(settings: Settings, tickers: Sequence[str] | None = None, *, adjudicate: bool = False,
                    adjudicate_passages: bool = False, max_usd: float = DEFAULT_MAX_USD, dry_run: bool = False,
                    llm: Callable[..., Any] | None = None, model: str | None = None, out_dir: Path | None = None,
                    align_params: AlignParams = AlignParams(),
                    adj_params: adj.AdjudicationParams = adj.AdjudicationParams(),
                    passage_params: PassageParams = PassageParams(),
                    pas_params: pad.PassageAdjudicationParams = pad.PassageAdjudicationParams(),
                    embed: sentence_embed.EmbedFn | None = None) -> AlignRun:
    """Align every consecutive annual pair of every ticker and write the three tables per ticker plus the provenance sidecar.

    Phase 1 (free, pure) aligns every pair. Phases 2 and 3 BUY missing model answers and only when a flag permits it: ``adjudicate``
    (the item level), ``adjudicate_passages`` (the passage layer's band sentences and, with ``passage_params.adjudicate_below_band``,
    below sentences; it needs ``embed`` unless ``dry_run``). The worst case is checked against ``max_usd`` BEFORE any call (raising
    ``adjudicate.BudgetExceeded``); a run without a flag makes no call whatever ``max_usd`` says. Phase 4 settles with EVERY recorded
    answer of both checkpoints, computes the passages and writes (module docstring: replay versus buying); the checkpoints are read
    again after any purchase, so the tables and the provenance digests describe the same bytes. ``dry_run`` stops after the estimates:
    nothing is written or embedded, no model is called, and passages are not computed. ``out_dir`` (default: the lake's
    ``risk_alignment`` directory) redirects the tables, both checkpoints, the sidecar AND the sentence-embedding cache (tests use it
    to leave the real files alone)."""
    directory = out_dir or alignment_dir(settings)
    tickers = discover_tickers(settings, tickers)
    if not tickers:
        raise AlignItemsError(f"no risk items in {items_dir(settings)}: run `semigraph risk-items` first")
    if adjudicate_passages and not dry_run and embed is None:
        raise AlignItemsError("passage adjudication (prompt pas-v3) shows the model the nearest sentences by embedding: pass `embed` "
                              "(the CLI does). Without it a buying run would record lexical-only answers under the semantic version")
    per_ticker = _align_all(settings, tickers, align_params)
    model, call = model or settings.adjudication_model, llm or adj.default_llm_json
    bought = _purchases(per_ticker, directory, model, call, adjudicate=adjudicate, adjudicate_passages=adjudicate_passages,
                        max_usd=max_usd, dry_run=dry_run, adj_params=adj_params, passage_params=passage_params, pas_params=pas_params,
                        embed=embed)
    flags = {"adjudicate": adjudicate, "adjudicate_passages": adjudicate_passages,
             "adjudicate_all_absent": passage_params.adjudicate_below_band}
    return _write_tables(settings, directory, per_ticker, bought, model, dry_run, flags, adj_params, passage_params, pas_params)


def _write_tables(settings: Settings, directory: Path, per_ticker: Mapping[str, Sequence[PairWork]], bought: _Purchases, model: str,
                  dry_run: bool, flags: Mapping[str, bool], adj_params: adj.AdjudicationParams, passage_params: PassageParams,
                  pas_params: pad.PassageAdjudicationParams) -> AlignRun:
    """Phase 4: settle every pair with the recorded answers, compute the passages and write the tables and the provenance."""
    item_cp, passage_cp = adj.Checkpoint(directory / adj.CHECKPOINT_NAME), adj.Checkpoint(directory / pad.CHECKPOINT_NAME)
    recorded = item_records(per_ticker, item_cp, model)
    band, replay_params = replay_context(passage_cp, model, pas_params, passage_params)
    counts = bought.passage.counts if bought.passage else {}
    written: list[Path] = []
    summary: list[dict] = []
    entries: dict[str, dict] = {}
    used = 0
    for ticker, works in per_ticker.items():
        outcomes = [finalize_pair(w, recorded.get(w.pair["pair_id"]), with_passages=not dry_run, adj_params=adj_params,
                                  passage_params=replay_params, band=band) for w in works]
        outcomes = [_with_counts(o, counts[pid], passage_params) if dry_run and (pid := o.work.pair["pair_id"]) in counts else o
                    for o in outcomes]
        applied = sum(o.band_applied for o in outcomes)
        used += applied
        summary += [summary_row(o, passages_computed=not dry_run) for o in outcomes]
        if not dry_run:
            written += write_ticker(directory, ticker, outcomes)
            entries[ticker] = provenance.ticker_entry(
                flags=flags, model=model, item_checkpoint=item_cp, passage_checkpoint=passage_cp,
                passage_version=band.version if band else None, below_zone=replay_params.adjudicate_below_band,
                item_verdicts=sum(len(recorded.get(w.pair["pair_id"], ())) for w in works), passage_verdicts=applied,
                inputs=ticker_input_paths(settings, ticker))
    sidecar = provenance.write_provenance(directory, entries) if entries else None
    step, item = bought.passage, bought.item
    return AlignRun(
        summary, item.estimate, written + ([sidecar] if sidecar else []), dry_run,
        passage_estimate=step.estimate if step else None, passage_budget_usd=bought.passage_budget, passage_verdicts_used=used,
        passage_calls=step.stats.calls if step else 0, passage_estimates=step.by_zone if step else {},
        passage_prompt_version=band.version if band else "", passage_failed=step.stats.failed if step else 0,
        item_verdicts_used=sum(len(r) for r in recorded.values()), item_calls=item.stats.calls, item_failed=item.stats.failed,
        item_charged_usd=item.stats.charged, below_replayed=band is not None and replay_params.adjudicate_below_band,
        provenance=sidecar)


# --------------------------------------------------------------------------
# the printed table
# --------------------------------------------------------------------------

_COLS = (("pair", "pair_id", 46, "<"), ("older", "older_items", 5, ">"), ("newer", "newer_items", 5, ">"),
         ("unch", "older_unchanged", 5, ">"), ("reword", "older_reworded", 6, ">"), ("merged", "older_merged", 6, ">"),
         ("REMOVED", "older_removed", 7, ">"), ("uncert", "older_uncertain", 6, ">"), ("NEW", "newer_new", 4, ">"),
         ("n-unc", "newer_uncertain", 5, ">"), ("p-rem", "passages_removed", 5, ">"), ("p-rew", "passages_reworded", 5, ">"),
         ("p-add", "passages_added", 5, ">"), ("adj", "adjudicated", 4, ">"), ("band", "band", 4, ">"),
         ("b-ans", "band_answered", 5, ">"), ("below", "below", 5, ">"), ("bl-ans", "below_answered", 6, ">"))


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
    bands = (f"; band sentences {totals['band']} ({totals['band_answered']} answered)"
             if any(r.get("band") is not None for r in rows if r["compared"]) else "")
    below = (f", below-band sentences {totals['below']} ({totals['below_answered']} answered)"
             if any(r.get("below") is not None for r in rows if r["compared"]) else "")
    lines.append(f"{len([r for r in rows if r['compared']])} pair(s) compared, {totals['not_compared']} not compared; "
                 f"removed items {totals['older_removed']}, new items {totals['newer_new']}, passages {passages}{bands}{below}")
    return "\n".join(lines)

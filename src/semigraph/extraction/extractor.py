"""LLM extraction over chunk parquets — ported from notebooks 07 and 12.

The paid stage. Battle scars preserved (each cost a failed run):

- extractor runs on ``settings.llm_model`` (Sonnet) via ``semigraph.llm.llm_json``
  with its default ``thinking_off=True``
- critic runs on ``settings.critic_model`` (Haiku 4.5) with ``thinking_off=False``
  and ``max_tokens=600``
- programmatic anti-fabrication gate BEFORE the critic: ``evidence_quote`` must
  appear verbatim in the chunk after whitespace/smart-quote normalization
- checkpointed per chunk: append-to-jsonl + flush after every chunk; resume by
  reading done ``chunk_id``s — interruptions never re-bill
- critic verdict-count mismatch -> keep all relations (as in the notebooks)
- :func:`estimate_extraction_cost` shows the honest cost estimate BEFORE any spend
"""

import json
import logging
from pathlib import Path

import pandas as pd

from ..artifacts import read_prompt
from ..config import Settings, get_settings
from ..llm import llm_json as _default_llm
from .gates import quote_in_chunk
from .schemas import ChunkExtraction, CriticVerdict, normalize_category

logger = logging.getLogger("semigraph.extraction")

from ..universe import FILERS, HIST_ANNUALS, RISK_SECTIONS  # noqa: E402,F401 — re-exported (single source: universe.py)
from ..versions import (  # noqa: E402
    annual_periods,
    compute_filing_versions,
    current_quarterly_accession,
    effective_annual_sections,
)

_SCOPE_COLUMNS = ["chunk_id", "ticker", "form", "accession_no", "section_id",
                  "filing_date", "n_tokens", "text", "section_title", "sub_heading"]

# Cost-estimate assumptions (token counts per chunk; measured on the NVDA PoC
# in notebook 12). Prices are NOT here — they come from Settings.
EXTRACT_OVERHEAD_TOKENS = 800        # instructions + JSON schema, per extractor call
EXTRACT_OUT_TOKENS_PER_CHUNK = 300   # mostly small/empty JSON
CRITIC_FRACTION = 0.25               # share of chunks with relations surviving the quote gate
CRITIC_OVERHEAD_TOKENS = 500         # critic instructions + claims, per critic call
CRITIC_OUT_TOKENS_PER_CHUNK = 40     # a short list of booleans
WORST_CASE_MULTIPLIER = 1.5


def chunk_parquet_path(settings: Settings, ticker: str) -> Path:
    """Chunk parquet for one filer (notebook 04's ``nvda_chunks.parquet`` reused as-is)."""
    name = "nvda_chunks.parquet" if ticker == "NVDA" else f"{ticker}_chunks.parquet"
    return settings.chunks_dir / name


def extractions_jsonl_path(settings: Settings, ticker: str) -> Path:
    """Extraction checkpoint jsonl — NVDA appends to the notebook-07 PoC file."""
    name = "nvda_extractions.jsonl" if ticker == "NVDA" else f"{ticker.lower()}_extractions.jsonl"
    return settings.extractions_dir / name


def _filing_versions(chunks: pd.DataFrame, annual_form: str, quarterly_form: str | None,
                     sections: dict[str, set[str]]):
    """Version status of every filing that has chunks (see ``semigraph.versions``).

    Only filings with content take part, so an unsegmentable filing (ASML's
    2023/24 20-Fs, a Part-III-only 10-K/A) never occupies a history slot or
    replaces an original. ``sections`` (accession -> section ids present) lets a
    partial amendment overlay only the sections it contains. Ordering is by
    ``filing_date`` (ties: ``accession_no``) — never by the accession string
    alone: filing agents change accession prefixes (MSFT), so the newest filing
    can have the smaller number.
    """
    filings = chunks[["accession_no", "form", "filing_date"]].drop_duplicates("accession_no")
    rows = [{"accession_no": r.accession_no, "form": r.form, "filing_date": str(r.filing_date)[:10]}
            for r in filings.itertuples()]
    return compute_filing_versions(rows, annual_form=annual_form, quarterly_form=quarterly_form,
                                   sections=sections)


def _rows_of(chunks: pd.DataFrame, accession: str, sections: frozenset[str] | set[str]) -> pd.DataFrame:
    """Chunks of ``accession`` restricted to ``sections`` (the ones it owns in its period)."""
    return chunks[(chunks["accession_no"] == accession) & chunks["section_id"].isin(sections)]


def extraction_scope(settings: Settings, ticker: str) -> pd.DataFrame:
    """Latest annual (its effective sections) + the current quarterly + HIST_ANNUALS
    prior annuals (risk-only). Ported from notebook 12, now version- and section-aware.

    - A 10-K/A that restates every section stands in for its original; one that
      restates only some (AMD's MD&A-only correction) is an overlay: those sections
      come from the amendment, the rest from the original.
    - A quarterly that a newer annual has rolled forward is no longer current
      and is not worth new spend.
    This scope governs NEW extraction spend only — the graph loader takes every
    chunk that already has an extraction record, so a filing that ages out of
    this scope keeps its history in the graph.
    """
    path = chunk_parquet_path(settings, ticker)
    if not path.exists():
        logger.warning("%s: no chunk parquet at %s — skipping", ticker, path)
        return pd.DataFrame(columns=_SCOPE_COLUMNS)
    ch = pd.read_parquet(path)
    if ch.empty or "form" not in ch.columns:  # filer with no segmentable chunks — skip gracefully
        return pd.DataFrame(columns=_SCOPE_COLUMNS)
    _, annual_form, quarterly_form = FILERS[ticker]
    sections = {acc: set(g["section_id"]) for acc, g in ch.groupby("accession_no")}
    versions = _filing_versions(ch, annual_form, quarterly_form, sections)
    owned = effective_annual_sections(versions, sections)
    periods = annual_periods(versions)
    parts = []
    if periods:
        parts += [_rows_of(ch, acc, owned.get(acc, ())) for acc in periods[-1]]        # latest: everything effective
        risk = RISK_SECTIONS[annual_form]
        for period in periods[-(1 + HIST_ANNUALS):-1]:                                   # history: risks only
            parts += [_rows_of(ch, acc, {risk} & owned.get(acc, frozenset())) for acc in period]
    current_q = current_quarterly_accession(versions) if quarterly_form else None
    if current_q:
        parts.append(ch[ch["accession_no"] == current_q])
    return pd.concat(parts, ignore_index=True).drop_duplicates("chunk_id") if parts else ch.iloc[0:0]


def load_done_chunk_ids(settings: Settings) -> set[str]:
    """Chunk ids already extracted, across every filer's checkpoint jsonl."""
    done: set[str] = set()
    for jl in settings.extractions_dir.glob("*_extractions.jsonl"):
        done |= {json.loads(line)["chunk_id"]
                 for line in jl.open(encoding="utf-8") if line.strip()}
    return done


def build_extraction_plan(
    settings: Settings, tickers: list[str] | None = None
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Return ``(scopes, todo)`` per ticker; ``todo`` excludes already-done chunks."""
    tickers = list(tickers) if tickers else list(FILERS)
    unknown = [t for t in tickers if t not in FILERS]
    if unknown:
        raise KeyError(f"unknown ticker(s) {unknown} — known filers: {sorted(FILERS)}")
    done = load_done_chunk_ids(settings) if settings.extractions_dir.exists() else set()
    scopes = {t: extraction_scope(settings, t) for t in tickers}
    todo = {t: s[~s["chunk_id"].isin(done)] for t, s in scopes.items()}
    return scopes, todo


def estimate_extraction_cost(
    todo: dict[str, pd.DataFrame], settings: Settings | None = None
) -> dict:
    """Honest cost estimate for the remaining work — ported from notebook 12.

    Per extractor call = instruction/schema overhead + chunk; the critic only
    runs on the ~25% of chunks whose relations survive the quote gate; average
    output measured from the NVDA PoC (~300 tokens, mostly small/empty JSON).
    Prices (USD per Mtok) come from ``settings`` (default: ``get_settings()``):
    ``llm_input/output_price_per_mtok`` for the extractor model and
    ``critic_input/output_price_per_mtok`` for the critic, so a model or
    price change never needs a code edit. The CLI must show this BEFORE any
    spend.
    """
    settings = settings or get_settings()
    n_todo = sum(len(s) for s in todo.values())
    chunk_tokens = int(sum(s["n_tokens"].sum() for s in todo.values() if len(s)))
    extract_in = ((n_todo * EXTRACT_OVERHEAD_TOKENS + chunk_tokens) / 1e6
                  * settings.llm_input_price_per_mtok)
    extract_out = (n_todo * EXTRACT_OUT_TOKENS_PER_CHUNK / 1e6
                   * settings.llm_output_price_per_mtok)
    critic_cost = CRITIC_FRACTION * (
        (n_todo * CRITIC_OVERHEAD_TOKENS + chunk_tokens) / 1e6 * settings.critic_input_price_per_mtok
        + n_todo * CRITIC_OUT_TOKENS_PER_CHUNK / 1e6 * settings.critic_output_price_per_mtok
    )
    likely = extract_in + extract_out + critic_cost
    return {
        "n_chunks": n_todo,
        "chunk_tokens": chunk_tokens,
        "extractor_in_usd": round(extract_in, 2),
        "extractor_out_usd": round(extract_out, 2),
        "critic_usd": round(critic_cost, 2),
        "likely_usd": round(likely, 2),
        "worst_case_usd": round(likely * WORST_CASE_MULTIPLIER, 2),
        "per_ticker": {t: len(s) for t, s in todo.items()},
    }


def run_extraction(
    settings: Settings,
    tickers: list[str] | None = None,
    llm=None,
) -> dict[str, int]:
    """Run the extractor + gates + critic loop over the remaining chunks.

    Ported from notebook 12 (Cell 12, the evolved multi-filer loop). ``llm``
    defaults to :func:`semigraph.llm.llm_json` and is injectable for tests —
    it must accept ``(prompt, model_cls, *, model=..., max_tokens=...,
    thinking_off=...)`` and return a validated ``model_cls`` instance.

    Checkpointed per chunk (append + flush); safe to interrupt and re-run —
    completed chunks are never re-billed. Risk-factor categories are
    normalized onto ``RISK_CATEGORIES`` at persist time (SDK improvement over
    the notebooks; see ``schemas.normalize_category``).

    Returns {ticker: number of chunks extracted this run}.
    """
    llm = llm or _default_llm
    _, todo = build_extraction_plan(settings, tickers)
    est = estimate_extraction_cost(todo, settings)
    logger.info(
        "extraction plan: %d chunks remaining (%s chunk tokens) — estimated cost ~$%.2f likely / ~$%.2f worst case",
        est["n_chunks"], f"{est['chunk_tokens']:,}", est["likely_usd"], est["worst_case_usd"],
    )

    extractor_prompt = read_prompt("extractor")
    critic_prompt = read_prompt("critic")
    schema_json = json.dumps(ChunkExtraction.model_json_schema(), indent=None)
    settings.extractions_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, int] = {}
    for ticker, batch in todo.items():
        if batch.empty:
            summary[ticker] = 0
            continue
        name = FILERS[ticker][0]
        out_path = extractions_jsonl_path(settings, ticker)
        logger.info("--- %s: %d chunks ---", ticker, len(batch))
        with out_path.open("a", encoding="utf-8") as sink:
            for n, (_, c) in enumerate(batch.iterrows(), 1):
                sub = c["sub_heading"] if isinstance(c["sub_heading"], str) else ""
                prompt = extractor_prompt.format(
                    ticker=c["ticker"], ticker_name=name, form=c["form"],
                    filing_date=c["filing_date"], section_title=c["section_title"],
                    sub_heading=f', sub-heading "{sub}"' if sub else "",
                    schema=schema_json, chunk_text=c["text"],
                )
                extraction = llm(prompt, ChunkExtraction, model=settings.llm_model)

                # Gate 1 (programmatic): evidence quotes must exist verbatim in the chunk
                relations = [r for r in extraction.relations if quote_in_chunk(r.evidence_quote, c["text"])]
                risks = [r for r in extraction.risk_factors if quote_in_chunk(r.evidence_quote, c["text"])]

                # Gate 2 (critic reflection): semantic support check on surviving relations
                if relations:
                    claims = "\n".join(
                        f"{j + 1}. {r.source_entity} {r.relation} {r.target_entity}"
                        for j, r in enumerate(relations)
                    )
                    verdict = llm(
                        critic_prompt.format(chunk_text=c["text"], claims=claims),
                        CriticVerdict, model=settings.critic_model,
                        max_tokens=600, thinking_off=False,
                    )
                    # verdict-count mismatch -> keep all relations (as in the notebooks)
                    kept = ([r for r, ok in zip(relations, verdict.verdicts) if ok]
                            if len(verdict.verdicts) == len(relations) else relations)
                else:
                    kept = []

                record = {
                    "chunk_id": c["chunk_id"], "ticker": ticker,
                    "accession_no": c["accession_no"], "section_id": c["section_id"],
                    "relations": [r.model_dump() for r in kept],
                    "risk_factors": [{**r.model_dump(), "category": normalize_category(r.category)}
                                     for r in risks],
                    "products": [p.model_dump() for p in extraction.products],
                }
                sink.write(json.dumps(record) + "\n")
                sink.flush()  # checkpoint: interruptions never re-bill
                if n % 25 == 0:
                    logger.info("  %d/%d", n, len(batch))
        summary[ticker] = len(batch)
    logger.info("extraction complete: %s", summary)
    return summary

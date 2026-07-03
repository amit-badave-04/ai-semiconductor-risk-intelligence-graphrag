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
from ..config import Settings
from ..llm import llm_json as _default_llm
from .gates import quote_in_chunk
from .schemas import ChunkExtraction, CriticVerdict, normalize_category

logger = logging.getLogger("semigraph.extraction")

# ticker: (canonical name, annual form, quarterly form or None) — ported from
# notebook 12. The canonical name is the {ticker_name} the extractor prompt
# tells the LLM to resolve "we"/"our" to.
FILERS: dict[str, tuple[str, str, str | None]] = {
    "NVDA": ("Nvidia", "10-K", "10-Q"), "AMD": ("AMD", "10-K", "10-Q"),
    "INTC": ("Intel", "10-K", "10-Q"), "AVGO": ("Broadcom", "10-K", "10-Q"),
    "QCOM": ("Qualcomm", "10-K", "10-Q"), "MU": ("Micron", "10-K", "10-Q"),
    "AAPL": ("Apple", "10-K", "10-Q"), "MSFT": ("Microsoft", "10-K", "10-Q"),
    "AMZN": ("Amazon", "10-K", "10-Q"), "GOOGL": ("Alphabet", "10-K", "10-Q"),
    "META": ("Meta", "10-K", "10-Q"),
    "TSM": ("TSMC", "20-F", None), "ASML": ("ASML", "20-F", None),
}

RISK_SECTIONS = {"10-K": "I.1A", "10-Q": "II.1A", "20-F": "I.3"}

# how many PRIOR annuals contribute risk-only chunks; 1 = latest + one prior
# = two time points per company for the bitemporal lineages (notebook 13)
HIST_ANNUALS = 1

_SCOPE_COLUMNS = ["chunk_id", "ticker", "form", "accession_no", "section_id",
                  "filing_date", "n_tokens", "text", "section_title", "sub_heading"]


def chunk_parquet_path(settings: Settings, ticker: str) -> Path:
    """Chunk parquet for one filer (notebook 04's ``nvda_chunks.parquet`` reused as-is)."""
    name = "nvda_chunks.parquet" if ticker == "NVDA" else f"{ticker}_chunks.parquet"
    return settings.chunks_dir / name


def extractions_jsonl_path(settings: Settings, ticker: str) -> Path:
    """Extraction checkpoint jsonl — NVDA appends to the notebook-07 PoC file."""
    name = "nvda_extractions.jsonl" if ticker == "NVDA" else f"{ticker.lower()}_extractions.jsonl"
    return settings.extractions_dir / name


def extraction_scope(settings: Settings, ticker: str) -> pd.DataFrame:
    """Latest annual (all kept sections) + latest quarterly + HIST_ANNUALS prior
    annuals (risk-only). Ported from notebook 12."""
    path = chunk_parquet_path(settings, ticker)
    if not path.exists():
        logger.warning("%s: no chunk parquet at %s — skipping", ticker, path)
        return pd.DataFrame(columns=_SCOPE_COLUMNS)
    ch = pd.read_parquet(path)
    if ch.empty or "form" not in ch.columns:  # filer with no segmentable chunks — skip gracefully
        return pd.DataFrame(columns=_SCOPE_COLUMNS)
    _, annual_form, quarterly_form = FILERS[ticker]
    annuals = sorted(ch[ch["form"] == annual_form]["accession_no"].unique(),
                     key=lambda a: ch[ch["accession_no"] == a]["filing_date"].iloc[0])
    parts = []
    if annuals:
        parts.append(ch[ch["accession_no"] == annuals[-1]])                      # latest annual: everything
        risk = RISK_SECTIONS[annual_form]
        hist = annuals[-(1 + HIST_ANNUALS):-1]
        parts.append(ch[ch["accession_no"].isin(hist) & (ch["section_id"] == risk)])  # history: risks only
    if quarterly_form:
        qs = ch[ch["form"] == quarterly_form]
        if len(qs):
            parts.append(qs[qs["accession_no"] == qs["accession_no"].max()])
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


def estimate_extraction_cost(todo: dict[str, pd.DataFrame]) -> dict:
    """Honest cost estimate for the remaining work — ported from notebook 12.

    Per extractor call = instruction/schema overhead + chunk; the critic
    (Haiku 4.5, $1/$5 per Mtok) only runs on the ~25% of chunks whose relations
    survive the quote gate; average output measured from the NVDA PoC
    (~300 tokens, mostly small/empty JSON). The CLI must show this BEFORE
    any spend.
    """
    n_todo = sum(len(s) for s in todo.values())
    chunk_tokens = int(sum(s["n_tokens"].sum() for s in todo.values() if len(s)))
    OVERHEAD, CRITIC_FRACTION, OUT_PER_CHUNK = 800, 0.25, 300
    extract_in = (n_todo * OVERHEAD + chunk_tokens) / 1e6 * 3
    extract_out = n_todo * OUT_PER_CHUNK / 1e6 * 15
    critic_cost = CRITIC_FRACTION * ((n_todo * 500 + chunk_tokens) / 1e6 * 1 + n_todo * 40 / 1e6 * 5)
    likely = extract_in + extract_out + critic_cost
    return {
        "n_chunks": n_todo,
        "chunk_tokens": chunk_tokens,
        "extractor_in_usd": round(extract_in, 2),
        "extractor_out_usd": round(extract_out, 2),
        "critic_usd": round(critic_cost, 2),
        "likely_usd": round(likely, 2),
        "worst_case_usd": round(likely * 1.5, 2),
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
    est = estimate_extraction_cost(todo)
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

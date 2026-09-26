"""The item layer against the REAL data lake (skipped when data/interim/risk_items is absent). No Neo4j, no network, no model.

* `align-items` on real filings reproduces the measured NVIDIA FY25 -> FY26 result of M1B_PLAN L.1 and is byte-identical on a re-run;
* every chunk id the retriever can cite for a company's CURRENT pair (its removed and new items, its passages and their counterpart
  chunks) is an EvidenceSpan in the REAL span scope of the build (`freshness.select_span_chunks`, extraction records + current scope
  + one prior annual): the verify step after a rebuild ("citable chunk ids of the current pair resolve") therefore passes.
"""

from pathlib import Path

import pandas as pd
import pytest

from semigraph.config import get_settings
from semigraph.graph import freshness, items, loaders
from semigraph.universe import HIST_ANNUALS

pytestmark = pytest.mark.skipif(not Path("data/interim/risk_items/NVDA_risk_items.parquet").exists(),
                                reason="the real risk-item lake (data/interim/risk_items) is not present")

FY25, FY26 = "0001045810-25-000023", "0001045810-26-000021"
# NVDA (a 10-K filer whose chunks are all spans), AMD (a partial 10-K/A overlay), AVGO (a corrected original), TSM (20-F)
TICKERS = ["NVDA", "AMD", "AVGO", "TSM"]


@pytest.fixture(scope="module")
def out_dir(tmp_path_factory):
    """The tables are written HERE, never over the real data/interim/risk_alignment (which may hold an adjudicated run)."""
    return tmp_path_factory.mktemp("risk_alignment")


@pytest.fixture(scope="module")
def aligned(out_dir):
    settings = get_settings()
    return settings, items.run_align_items(settings, TICKERS, out_dir=out_dir)


def read(out_dir, ticker, name):
    return pd.read_parquet(items.table_path(out_dir, ticker, name))


def test_the_measured_nvidia_fy25_to_fy26_result_is_reproduced(aligned, out_dir):
    settings, run = aligned
    row = next(r for r in run.summary if r["pair_id"].endswith(f"{FY25}-{FY26}"))
    assert (row["older_unchanged"], row["older_reworded"], row["older_removed"], row["newer_new"], row["newer_carried"]) == (7, 16, 0, 1, 23)
    dec = read(out_dir, "NVDA", "decisions")
    new = dec[(dec["side"] == "newer") & (dec["label"] == "new") & dec["item_id"].str.startswith(FY26)]
    assert len(new) == 1
    passages = read(out_dir, "NVDA", "passages")
    removed = passages[(passages["kind"] == "removed") & passages["pair_id"].str.endswith(f"{FY25}-{FY26}")]
    assert removed["text"].str.contains("Notified Advanced Computing").any() and removed["text"].str.contains("out of China and Hong Kong").any()


def test_a_second_real_run_is_byte_identical(aligned, out_dir):
    settings, _ = aligned
    before = {p.name: p.read_bytes() for p in sorted(out_dir.glob("*.parquet"))}
    assert len(before) == 3 * len(TICKERS)
    items.run_align_items(settings, TICKERS, out_dir=out_dir)
    assert {p.name: p.read_bytes() for p in sorted(out_dir.glob("*.parquet"))} == before


@pytest.mark.parametrize("ticker", TICKERS)
def test_every_citable_chunk_of_the_current_pair_is_in_the_real_span_scope(aligned, out_dir, ticker):
    settings, _ = aligned
    manifest = loaders._load_manifest(settings)
    ch = loaders._read_chunks(settings, ticker)
    versions, states = loaders._ticker_freshness(manifest, ticker, ch)
    extracted = {r["chunk_id"] for r in loaders._load_extraction_records(settings, ticker) or []}
    scope = set(freshness.select_span_chunks(ch, ticker, extracted, versions, HIST_ANNUALS)["chunk_id"])
    pairs, dec, passages = (read(out_dir, ticker, n) for n in ("pairs", "decisions", "passages"))
    it = pd.read_parquet(items.items_dir(settings) / f"{ticker}_risk_items.parquet").set_index("item_id")
    pair = [p for p in pairs.itertuples() if states[p.newer_accession].is_current][-1]
    assert pair.comparable
    cited: set[str] = set()
    for item_id in dec[(dec["pair_id"] == pair.pair_id) & (((dec["side"] == "older") & (dec["label"] == "removed")) |
                                                            ((dec["side"] == "newer") & (dec["label"] == "new")))]["item_id"]:
        cited |= set(it.loc[item_id, "chunk_ids"])
    for p in passages[passages["pair_id"] == pair.pair_id].itertuples():
        cited |= set(p.chunk_ids) | set(p.counterpart_chunk_ids)
    assert cited and not (cited - scope), sorted(cited - scope)[:5]

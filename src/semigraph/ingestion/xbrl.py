"""SEC XBRL companyfacts ingestion + deterministic metric curation
(ported from notebook 02, generalized to all filers in notebook 12).

**The project's cardinal rule: no LLM ever parses financial numbers.**
All quantitative facts (revenue, capex, R&D, net income) come
deterministically from the SEC XBRL Company Facts API
(``data.sec.gov/api/xbrl/companyfacts/CIK##########.json``).

Paths (identical to the notebooks):

- raw cache:  ``data/raw/xbrl/CIK##########_companyfacts.json``
- curated:    ``data/processed/xbrl/<TICKER>_key_metrics.parquet``

Taxonomy quirks the curation handles (notebook 02 data dictionary):

1. concept drift — each metric maps to a concept *list* (e.g. ``Revenues``
   vs ASC-606 ``RevenueFromContractWithCustomerExcludingAssessedTax``);
   US filers report under us-gaap, TSMC/ASML under ifrs-full
2. comparative re-reporting — the same fact appears in >=3 filings; we
   dedupe by period ``end`` keeping the earliest ``filed`` (first
   disclosure = the citable event)
3. fiscal-year offsets (Nvidia's FY ends late January) — we keep explicit
   ``start``/``end`` dates, never bare FY labels
4. full-year periods only — 10-Ks restate quarters too, so periods
   shorter than 300 days are dropped
"""

import json
import logging
import time
import urllib.request
from pathlib import Path

import pandas as pd

from ..config import Settings, get_settings
from .edgar import FILERS, _identity, load_manifest, load_ticker_to_cik

logger = logging.getLogger("semigraph.ingestion.xbrl")

# metric -> concept list spanning us-gaap AND ifrs-full (notebook 12)
KEY_CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenue",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    ],
    "rnd": [
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenditure",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
}
ANNUAL_FORMS = {"10-K", "20-F"}

CURATED_COLUMNS = [
    "metric", "concept", "start", "end", "val", "unit", "accn", "ticker", "cik",
]


def xbrl_raw_dir(settings: Settings) -> Path:
    return settings.raw_dir / "xbrl"


def xbrl_out_dir(settings: Settings) -> Path:
    return settings.processed_dir / "xbrl"


def download_companyfacts(settings: Settings | None = None, cik: int = 0) -> dict:
    """Fetch (or reuse) the raw companyfacts JSON for one CIK — immutable input.

    Ported from notebook 12 ``fetch_companyfacts``; SEC_USER_AGENT is sent
    on the request and a 0.15 s sleep respects the 10 req/s policy.
    """
    settings = settings or get_settings()
    raw = xbrl_raw_dir(settings) / f"CIK{cik:010d}_companyfacts.json"
    if not raw.exists():
        raw.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
        logger.info("downloading %s", url)
        req = urllib.request.Request(
            url, headers={"User-Agent": _identity(settings)}
        )
        raw.write_bytes(urllib.request.urlopen(req).read())
        time.sleep(0.15)
    return json.loads(raw.read_text(encoding="utf-8"))


def curate_metrics(facts_json: dict, ticker: str, cik: int) -> pd.DataFrame:
    """Deterministically curate key annual metrics from a companyfacts dict.

    Pure transformation (no filesystem/network) — ported from notebook 12
    ``curate_metrics``. One row per (metric, period end), first disclosure
    wins, full-year periods only. Columns: ``CURATED_COLUMNS``.
    """
    rows = []
    for taxonomy in ("us-gaap", "ifrs-full"):
        for concept, payload in facts_json["facts"].get(taxonomy, {}).items():
            for unit, facts in payload["units"].items():
                for fact in facts:
                    rows.append(
                        {
                            "concept": concept,
                            "unit": unit,
                            **{
                                k: fact.get(k)
                                for k in (
                                    "start", "end", "val", "accn",
                                    "fy", "fp", "form", "filed",
                                )
                            },
                        }
                    )
    df = pd.DataFrame(rows)
    out = []
    if not df.empty:
        for metric, concepts in KEY_CONCEPTS.items():
            sel = df[
                df["concept"].isin(concepts)
                & df["form"].isin(ANNUAL_FORMS)
                & df["start"].notna()
            ].copy()
            if sel.empty:
                continue
            sel["days"] = (
                pd.to_datetime(sel["end"]) - pd.to_datetime(sel["start"])
            ).dt.days
            # keep full-year periods only; first disclosure per period end
            sel = (
                sel[sel["days"] > 300]
                .sort_values("filed")
                .groupby("end", as_index=False)
                .first()
            )
            sel.insert(0, "metric", metric)
            sel["ticker"], sel["cik"] = ticker, cik
            out.append(sel[CURATED_COLUMNS])
    if not out:
        return pd.DataFrame(columns=CURATED_COLUMNS)
    return pd.concat(out, ignore_index=True)


def extract_metrics(
    settings: Settings | None = None, tickers: list[str] | None = None
) -> dict:
    """Download companyfacts + curate key metrics for the universe
    (idempotent; ported from notebook 12 stage 2).

    Writes ``data/processed/xbrl/<TICKER>_key_metrics.parquet`` per filer;
    a filer whose parquet already exists is skipped. CIKs come from the
    acquisition manifest when available, else from ``company_tickers.json``.

    Returns ``{ticker: {"rows": n, "cached": bool, "metrics": [...]}}``.
    """
    settings = settings or get_settings()
    manifest = load_manifest(settings)
    tickers = list(tickers) if tickers else list(FILERS)
    out_dir = xbrl_out_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)

    ticker_to_cik: dict[str, int] | None = None
    summary: dict[str, dict] = {}
    for ticker in tickers:
        out_path = out_dir / f"{ticker}_key_metrics.parquet"
        if out_path.exists():
            cached = pd.read_parquet(out_path)
            logger.info("%s: metrics cached (%d rows)", ticker, len(cached))
            summary[ticker] = {
                "rows": len(cached),
                "cached": True,
                "metrics": sorted(cached["metric"].unique()) if len(cached) else [],
            }
            continue
        if ticker in manifest:
            cik = int(manifest[ticker][0]["cik"])
        else:
            if ticker_to_cik is None:
                ticker_to_cik = load_ticker_to_cik(settings)
            cik = ticker_to_cik[ticker]
        facts_json = download_companyfacts(settings, cik)
        curated = curate_metrics(facts_json, ticker, cik)
        curated.to_parquet(out_path, index=False)
        metrics = sorted(curated["metric"].unique()) if len(curated) else []
        logger.info(
            "%s: %d metric-periods (%s)",
            ticker, len(curated), ", ".join(metrics) or "none",
        )
        summary[ticker] = {"rows": len(curated), "cached": False, "metrics": metrics}
    return summary

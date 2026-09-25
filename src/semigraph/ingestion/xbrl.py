"""SEC XBRL companyfacts ingestion + deterministic metric curation
(ported from notebook 02, generalized to all filers in notebook 12).

**The project's cardinal rule: no LLM ever parses financial numbers.**
All quantitative facts (revenue, capex, R&D, net income) come
deterministically from the SEC XBRL Company Facts API
(``data.sec.gov/api/xbrl/companyfacts/CIK##########.json``) and, where that
API lags (see below), from the filing's own inline XBRL via edgartools.

Paths (identical to the notebooks):

- raw cache:  ``data/raw/xbrl/CIK##########_companyfacts.json``
- curated:    ``data/processed/xbrl/<TICKER>_key_metrics.parquet``

Taxonomy quirks the curation handles (notebook 02 data dictionary):

1. concept drift — each metric maps to a concept *list* (e.g. ``Revenues``
   vs ASC-606 ``RevenueFromContractWithCustomerExcludingAssessedTax``, or
   IFRS ``Revenue`` vs ``RevenueFromContractsWithCustomers``); US filers
   report under us-gaap, TSMC under ifrs-full (TWD) and ASML under us-gaap in
   EUR
2. comparative re-reporting — the same fact appears in >=3 filings; we
   dedupe by period ``end`` keeping the earliest ``filed`` (first
   disclosure = the citable event)
3. fiscal-year offsets (Nvidia's FY ends late January) — we keep explicit
   ``start``/``end`` dates, never bare FY labels
4. full-year periods only — 10-Ks restate quarters too, so periods
   shorter than 300 days are dropped
5. native units — a row keeps the unit it was reported in (USD, TWD, EUR).
   When one filing reports both the native currency and a USD convenience
   translation for the same period (TSMC does), the ticker's dominant unit
   wins deterministically; v1 picked one arbitrarily and stored TSM FY2021
   in USD among TWD rows.

Known gap (M1): the live Company Facts API has no ifrs-full facts for TSM
after FY2024 although the FY2025 20-F (0001628280-26-025362) is on EDGAR with
inline XBRL. ``supplement_metrics_from_filing_xbrl`` reads that filing's XBRL
through edgartools and appends the filing's own fiscal year — only after the
comparative periods it also reports agree with the curated Company Facts rows.
When a gap cannot be filled, ``extract_metrics`` logs a WARNING instead of
guessing.
"""

import json
import logging
import math
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pandas as pd

from ..config import Settings, get_settings
from .edgar import FILERS, _identity, _sec_get, atomic_write_text, load_manifest, load_ticker_to_cik

logger = logging.getLogger("semigraph.ingestion.xbrl")

SEC_PAUSE_S = 0.15  # stay well under SEC's 10 req/s

# metric -> concept list spanning us-gaap AND ifrs-full (notebook 12). Order is
# the tie-break priority when one filing tags the same period under >1 concept.
KEY_CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenue",
        "RevenueFromContractsWithCustomers",  # ifrs-full; TSMC's FY2025 20-F uses it, not Revenue
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
MIN_ANNUAL_DAYS = 300  # full-year periods only

CURATED_COLUMNS = [
    "metric", "concept", "start", "end", "val", "unit", "accn", "ticker", "cik",
]

_TAXONOMIES = ("us-gaap", "ifrs-full")
_FACT_KEYS = ("start", "end", "val", "accn", "fy", "fp", "form", "filed")
_CONCEPT_TO_METRIC = {c: m for m, cs in KEY_CONCEPTS.items() for c in cs}
_CONCEPT_RANK = {c: i for cs in KEY_CONCEPTS.values() for i, c in enumerate(cs)}
_METRIC_RANK = {m: i for i, m in enumerate(KEY_CONCEPTS)}
_OVERLAP_REL_TOL = 5e-3  # the project's numeric tolerance (+-0.5%)

FilingFactsLoader = Callable[[str, str], pd.DataFrame]


def xbrl_raw_dir(settings: Settings) -> Path:
    return settings.raw_dir / "xbrl"


def xbrl_out_dir(settings: Settings) -> Path:
    return settings.processed_dir / "xbrl"


# ------------------------------------------------------------- download

def download_companyfacts(
    settings: Settings | None = None,
    cik: int = 0,
    refresh: bool = False,
    *,
    fetch: Callable[[str], bytes] | None = None,
) -> dict:
    """Fetch (or reuse) the raw companyfacts JSON for one CIK — immutable input.

    ``refresh=True`` re-downloads it (a failed refresh keeps the previous file).
    ``fetch(url) -> bytes`` is the network seam; the real path sends
    SEC_USER_AGENT and sleeps 0.15 s to respect the 10 req/s policy.
    """
    settings = settings or get_settings()
    raw = xbrl_raw_dir(settings) / f"CIK{cik:010d}_companyfacts.json"
    if refresh or not raw.exists():
        raw.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
        logger.info("downloading %s", url)
        if fetch is not None:
            payload = fetch(url)
        else:
            payload = _sec_get(url, _identity(settings))
            time.sleep(SEC_PAUSE_S)
        atomic_write_text(raw, payload.decode("utf-8"))
    return json.loads(raw.read_text(encoding="utf-8"))


# ------------------------------------------------------------- curation

def _flatten_key_facts(facts_json: dict) -> pd.DataFrame:
    """One row per reported fact of every key concept, in both taxonomies."""
    rows = []
    for taxonomy in _TAXONOMIES:
        for concept, payload in facts_json["facts"].get(taxonomy, {}).items():
            if concept not in _CONCEPT_TO_METRIC:
                continue
            for unit, facts in payload["units"].items():
                for fact in facts:
                    rows.append(
                        {
                            "metric": _CONCEPT_TO_METRIC[concept],
                            "concept": concept,
                            "unit": unit,
                            **{k: fact.get(k) for k in _FACT_KEYS},
                        }
                    )
    return pd.DataFrame(rows)


def _dominant_unit(facts: pd.DataFrame) -> str:
    """The unit most facts are reported in (ties broken alphabetically)."""
    counts = facts["unit"].value_counts()
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def curate_metrics(facts_json: dict, ticker: str, cik: int) -> pd.DataFrame:
    """Deterministically curate key annual metrics from a companyfacts dict.

    Pure transformation (no filesystem/network). One row per (metric, period
    end), first disclosure wins, full-year periods only, native unit kept.
    Ties on ``filed`` (same filing, e.g. TWD + USD convenience translation, or
    two concepts) are broken deterministically: dominant unit, then
    ``KEY_CONCEPTS`` order. Columns: ``CURATED_COLUMNS``; rows ordered by
    metric (``KEY_CONCEPTS`` order) then period end.
    """
    facts = _flatten_key_facts(facts_json)
    if facts.empty:
        return pd.DataFrame(columns=CURATED_COLUMNS)
    annual = facts[facts["form"].isin(ANNUAL_FORMS) & facts["start"].notna()].copy()
    annual["days"] = (pd.to_datetime(annual["end"]) - pd.to_datetime(annual["start"])).dt.days
    annual = annual[annual["days"] > MIN_ANNUAL_DAYS]
    if annual.empty:
        return pd.DataFrame(columns=CURATED_COLUMNS)
    native = _dominant_unit(annual)
    ranked = annual.assign(
        _unit_rank=(annual["unit"] != native).astype(int),
        _concept_rank=annual["concept"].map(_CONCEPT_RANK),
    ).sort_values(
        ["filed", "_unit_rank", "_concept_rank", "unit", "accn"], kind="stable"
    )
    first = ranked.drop_duplicates(["metric", "end"], keep="first")
    ordered = first.assign(_metric_rank=first["metric"].map(_METRIC_RANK)).sort_values(
        ["_metric_rank", "end"], kind="stable"
    )
    out = ordered.assign(val=ordered["val"].astype("float64"), ticker=ticker, cik=int(cik))
    return out[CURATED_COLUMNS].reset_index(drop=True)


# ------------------------------------------------ gap detection + supplement

def filings_without_curated_metrics(
    manifest_rows: Sequence[dict], curated: pd.DataFrame
) -> list[dict]:
    """Original annual filings (10-K / 20-F; amendments and quarterlies never
    count) in the manifest whose accession first-discloses no curated row —
    i.e. Company Facts lacks that fiscal year. Oldest first."""
    have = set(curated["accn"].dropna()) if len(curated) else set()
    gaps = [
        row for row in manifest_rows
        if row["form"] in ANNUAL_FORMS and row["accession_no"] not in have
    ]
    return sorted(gaps, key=lambda r: (r["filing_date"], r["accession_no"]))


_FACT_COLUMNS = (
    "concept", "numeric_value", "currency", "period_type", "period_start", "period_end", "is_dimensioned",
)


def _filing_key_facts(facts: pd.DataFrame) -> pd.DataFrame:
    """Key-concept, non-dimensioned, full-year, monetary facts of one filing
    (edgartools ``facts.to_dataframe()`` shape), with local concept names."""
    if facts is None or any(c not in facts.columns for c in _FACT_COLUMNS):
        return pd.DataFrame(columns=["metric", "concept", "start", "end", "val", "unit"])
    df = facts[list(_FACT_COLUMNS)].copy()
    df["is_dimensioned"] = df["is_dimensioned"].fillna(False).astype(bool)
    df = df[
        ~df["is_dimensioned"]
        & df["period_type"].eq("duration")
        & df["numeric_value"].notna()
        & df["currency"].notna()
        & df["period_start"].notna()
        & df["concept"].astype(str).str.contains(":", regex=False)
    ]
    parts = df["concept"].astype(str).str.split(":", n=1, expand=True)
    df = df[parts[0].isin(_TAXONOMIES) & parts[1].isin(_CONCEPT_TO_METRIC)]
    local = df["concept"].astype(str).str.split(":", n=1).str[1]
    days = (pd.to_datetime(df["period_end"]) - pd.to_datetime(df["period_start"])).dt.days
    df = df.assign(metric=local.map(_CONCEPT_TO_METRIC), concept=local)[days > MIN_ANNUAL_DAYS]
    return df.rename(
        columns={"period_start": "start", "period_end": "end", "numeric_value": "val", "currency": "unit"}
    )[["metric", "concept", "start", "end", "val", "unit"]]


def _dominant_currency(key_facts: pd.DataFrame) -> str:
    """The currency reported for the most distinct (metric, period) pairs —
    the presentation currency; a one-period USD convenience translation loses."""
    pairs = key_facts.drop_duplicates(["unit", "metric", "end"]).groupby("unit").size()
    return sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _pick_period_facts(native: pd.DataFrame, accession_no: str) -> pd.DataFrame:
    """One fact per (metric, period): the first ``KEY_CONCEPTS`` concept that has
    any; a period whose chosen concept reports conflicting values is skipped
    (never guessed)."""
    picked = []
    for (metric, end), grp in native.groupby(["metric", "end"], sort=True):
        ranks = grp["concept"].map(_CONCEPT_RANK)
        cand = grp[ranks == ranks.min()]
        values = sorted(set(cand["val"]))
        if len(values) > 1:
            logger.warning(
                "%s: %s %s reports conflicting values %s — skipped, not guessed",
                accession_no, metric, end, values,
            )
            continue
        picked.append(cand.iloc[0])
    return pd.DataFrame(picked) if picked else native.iloc[0:0]


def _cross_check(
    picked: pd.DataFrame, curated: pd.DataFrame, current_end: str, unit: str, accession_no: str
) -> bool:
    """True when the comparative periods the filing repeats agree (+-0.5%) with
    the curated Company Facts rows, and at least one could be compared."""
    prior = picked[picked["end"] != current_end]
    existing = curated[curated["unit"] == unit].set_index(["metric", "end"])["val"]
    compared, mismatched = 0, []
    for row in prior.itertuples():
        if (row.metric, row.end) in existing.index:
            compared += 1
            curated_val = float(existing[(row.metric, row.end)])
            if not math.isclose(curated_val, float(row.val), rel_tol=_OVERLAP_REL_TOL):
                mismatched.append(f"{row.metric} {row.end}: curated {curated_val:,.0f} vs filing {float(row.val):,.0f}")
    if mismatched:
        logger.warning(
            "%s: filing XBRL disagrees with curated Company Facts %s — not appended", accession_no, mismatched
        )
        return False
    if compared == 0:
        logger.warning(
            "%s: no comparative period to cross-check against curated data — not appended", accession_no
        )
        return False
    return True


def _rows_to_append(
    picked: pd.DataFrame, curated: pd.DataFrame, current_end: str, ticker: str, cik: int, accession_no: str
) -> pd.DataFrame:
    """The filing's own fiscal-year rows not already curated, as ``CURATED_COLUMNS``."""
    have = set(zip(curated["metric"], curated["end"], strict=True)) if len(curated) else set()
    current = picked[picked["end"] == current_end]
    for metric in KEY_CONCEPTS:
        if metric not in set(current["metric"]) and (metric, current_end) not in have:
            logger.warning("%s: filing XBRL has no usable %s for %s — skipped", accession_no, metric, current_end)
    new = current[[(m, e) not in have for m, e in zip(current["metric"], current["end"], strict=True)]]
    rows = new.assign(
        accn=accession_no, ticker=ticker, cik=int(cik), val=new["val"].astype("float64"),
        _rank=new["metric"].map(_METRIC_RANK),
    ).sort_values(["_rank", "end"], kind="stable")
    return rows[CURATED_COLUMNS].reset_index(drop=True)


def supplement_metrics_from_filing_xbrl(
    curated: pd.DataFrame,
    facts: pd.DataFrame,
    *,
    ticker: str,
    cik: int,
    accession_no: str,
) -> pd.DataFrame:
    """Append one filing's own fiscal year, read from its inline XBRL.

    ``facts`` is edgartools' ``filing.xbrl().facts.to_dataframe()`` (injectable,
    so this stays pure). Semantics match ``curate_metrics``: same columns, native
    currency (never a USD convenience translation), full-year non-dimensioned
    facts only, first disclosure wins — comparative periods the filing repeats are
    *not* re-attributed to it, they are only used to cross-check that the
    filing's numbers agree with the curated Company Facts rows (+-0.5%). No
    overlap to check, or any disagreement, returns ``curated`` unchanged with a
    WARNING. A metric the filing does not tag, or tags with conflicting values,
    is skipped, never guessed. Returns a new frame; inputs are not mutated.
    """
    base = curated.copy()
    key = _filing_key_facts(facts)
    if key.empty:
        logger.warning("%s: filing XBRL has no usable key-concept facts", accession_no)
        return base
    unit = _dominant_currency(key)
    picked = _pick_period_facts(key[key["unit"] == unit], accession_no)
    if picked.empty:
        return base
    current_end = picked["end"].max()
    if not _cross_check(picked, base, current_end, unit, accession_no):
        return base
    rows = _rows_to_append(picked, base, current_end, ticker, cik, accession_no)
    if rows.empty:
        return base
    logger.info("%s: appended %d metric-period(s) for %s from filing XBRL", accession_no, len(rows), current_end)
    return pd.concat([base, rows], ignore_index=True) if len(base) else rows


def _edgartools_filing_facts(settings: Settings, ticker: str, accession_no: str) -> pd.DataFrame:
    """The real loader: the filing's inline-XBRL facts via edgartools."""
    import edgar  # heavy import kept local

    edgar.set_identity(_identity(settings))
    filings = edgar.Company(ticker).get_filings(accession_number=accession_no)
    if len(filings) == 0:
        raise LookupError(f"{ticker}: accession {accession_no} not found on EDGAR")
    xbrl = filings[0].xbrl()
    if xbrl is None:
        raise LookupError(f"{ticker}: accession {accession_no} has no inline XBRL")
    return xbrl.facts.to_dataframe()


def _fill_gaps(
    ticker: str,
    cik: int,
    manifest_rows: Sequence[dict],
    curated: pd.DataFrame,
    loader: FilingFactsLoader,
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Try to close every annual-filing gap from filing XBRL; warn about the rest.

    Returns ``(curated, supplemented accessions, still-missing accessions)``.
    """
    supplemented: list[str] = []
    for gap in filings_without_curated_metrics(manifest_rows, curated):
        accession = gap["accession_no"]
        try:
            updated = supplement_metrics_from_filing_xbrl(
                curated, loader(ticker, accession), ticker=ticker, cik=cik, accession_no=accession
            )
        except Exception as e:  # noqa: BLE001 — best-effort supplement: never abort curation
            logger.warning("%s: filing-XBRL supplement for %s failed: %s", ticker, accession, e)
            continue
        if len(updated) > len(curated):
            supplemented.append(accession)
            curated = updated
    remaining = filings_without_curated_metrics(manifest_rows, curated)
    for gap in remaining:
        logger.warning(
            "%s: annual filing %s (%s, filed %s) has no curated metric period — Company Facts "
            "lacks it and the filing-XBRL supplement could not fill it; answers about that fiscal "
            "year's revenue/capex/R&D/net income are unavailable",
            ticker, gap["accession_no"], gap["form"], gap["filing_date"],
        )
    return curated, supplemented, [g["accession_no"] for g in remaining]


def _cached_summary(out_path: Path, ticker: str) -> dict:
    cached = pd.read_parquet(out_path)
    logger.info("%s: metrics cached (%d rows)", ticker, len(cached))
    return {
        "rows": len(cached),
        "cached": True,
        "metrics": sorted(cached["metric"].unique()) if len(cached) else [],
    }


def _curate_ticker(
    settings: Settings,
    ticker: str,
    cik: int,
    manifest_rows: Sequence[dict],
    refresh: bool,
    fetch: Callable[[str], bytes] | None,
    loader: FilingFactsLoader,
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Download (or reuse) companyfacts, curate, then close annual-filing gaps.

    Returns ``(curated, supplemented accessions, still-missing accessions)``."""
    facts_json = download_companyfacts(settings, cik, refresh, fetch=fetch)
    curated = curate_metrics(facts_json, ticker, cik)
    return _fill_gaps(ticker, cik, manifest_rows, curated, loader)


def extract_metrics(
    settings: Settings | None = None,
    tickers: list[str] | None = None,
    refresh: bool = False,
    *,
    fetch: Callable[[str], bytes] | None = None,
    filing_facts_loader: FilingFactsLoader | None = None,
) -> dict:
    """Download companyfacts + curate key metrics for the universe
    (idempotent; ported from notebook 12 stage 2).

    Writes ``data/processed/xbrl/<TICKER>_key_metrics.parquet`` per filer; a
    filer whose parquet already exists is skipped unless ``refresh=True``, which
    re-downloads the raw JSON and re-curates. CIKs come from the acquisition
    manifest when available, else from ``company_tickers.json``. After curating,
    any original annual filing in the manifest that Company Facts does not
    cover is filled from the filing's inline XBRL (``filing_facts_loader``, by
    default edgartools) or reported with a WARNING.

    Returns ``{ticker: {"rows": n, "cached": bool, "metrics": [...]}}``; a
    freshly curated ticker also carries ``"supplemented"`` (accessions filled
    from filing XBRL) and ``"gaps"`` (annual accessions still without metrics).
    """
    settings = settings or get_settings()
    manifest = load_manifest(settings)
    tickers = list(tickers) if tickers else list(FILERS)
    out_dir = xbrl_out_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    loader = filing_facts_loader or (
        lambda t, a: _edgartools_filing_facts(settings, t, a)
    )

    ticker_to_cik: dict[str, int] | None = None
    summary: dict[str, dict] = {}
    for ticker in tickers:
        out_path = out_dir / f"{ticker}_key_metrics.parquet"
        if out_path.exists() and not refresh:
            summary[ticker] = _cached_summary(out_path, ticker)
            continue
        if ticker in manifest:
            cik = int(manifest[ticker][0]["cik"])
        else:
            if ticker_to_cik is None:
                ticker_to_cik = load_ticker_to_cik(settings)
            cik = ticker_to_cik[ticker]
        curated, supplemented, gaps = _curate_ticker(
            settings, ticker, cik, manifest.get(ticker, []), refresh, fetch, loader
        )
        curated.to_parquet(out_path, index=False)
        metrics = sorted(curated["metric"].unique()) if len(curated) else []
        logger.info(
            "%s: %d metric-periods (%s)",
            ticker, len(curated), ", ".join(metrics) or "none",
        )
        summary[ticker] = {
            "rows": len(curated), "cached": False, "metrics": metrics,
            "supplemented": supplemented, "gaps": gaps,
        }
    return summary

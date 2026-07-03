"""SEC EDGAR filing acquisition (ported from notebooks 01 and 12).

Downloads each filer's annual reports (filed >= ``ANNUAL_SINCE``) plus the
latest quarterly report via edgartools, persisting raw HTML to::

    data/raw/edgar/<TICKER>/<FORM>_<filing_date>_<accession_no>.html

(``/`` in form names becomes ``-``, e.g. ``10-K/A`` -> ``10-K-A``) and
recording provenance in ``data/raw/edgar/manifest_universe.json`` — the
exact on-disk layout the notebooks created, so the pre-existing data lake
stays readable and already-downloaded filings are never re-fetched.

Form-type reality (notebook 01's filing survey):

- US filers file 10-K (annual) / 10-Q (quarterly)
- TSMC and ASML are foreign private issuers filing **20-F** (annual only)
- **Samsung does not file with the SEC** — it exists in the graph only as
  an entity mentioned by others, hence it is absent from ``FILERS``

SEC etiquette: a declared identity (``SEC_USER_AGENT``) is sent on every
request, and downloads sleep 0.15 s to stay well under 10 req/s.
"""

import json
import logging
import time
import urllib.request
from pathlib import Path

from ..config import Settings, get_settings

logger = logging.getLogger("semigraph.ingestion.edgar")

# ticker: (canonical name, annual form, quarterly form or None) — notebook 12
FILERS: dict[str, tuple[str, str, str | None]] = {
    "NVDA": ("Nvidia", "10-K", "10-Q"),
    "AMD": ("AMD", "10-K", "10-Q"),
    "INTC": ("Intel", "10-K", "10-Q"),
    "AVGO": ("Broadcom", "10-K", "10-Q"),
    "QCOM": ("Qualcomm", "10-K", "10-Q"),
    "MU": ("Micron", "10-K", "10-Q"),
    "AAPL": ("Apple", "10-K", "10-Q"),
    "MSFT": ("Microsoft", "10-K", "10-Q"),
    "AMZN": ("Amazon", "10-K", "10-Q"),
    "GOOGL": ("Alphabet", "10-K", "10-Q"),
    "META": ("Meta", "10-K", "10-Q"),
    "TSM": ("TSMC", "20-F", None),
    "ASML": ("ASML", "20-F", None),
}

# Annuals filed this calendar year or later (covers the AI capex supercycle)
ANNUAL_SINCE = 2023

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def project_root(settings: Settings) -> Path:
    """The directory the notebooks called PROJECT_ROOT — parent of data_dir.

    Manifest ``local_path`` values are stored relative to it (the notebooks'
    convention), so joining ``project_root / local_path`` re-resolves them.
    """
    return settings.data_dir.resolve().parent


def _identity(settings: Settings) -> str:
    ua = settings.sec_user_agent.strip()
    if not ua:
        raise RuntimeError(
            "SEC_USER_AGENT is not set — SEC fair-access policy requires a "
            "declared identity (e.g. 'Jane Doe jane@example.com') in .env"
        )
    return ua


def _sec_get(url: str, user_agent: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    return urllib.request.urlopen(req).read()


def edgar_dir(settings: Settings) -> Path:
    return settings.raw_dir / "edgar"


def manifest_path(settings: Settings) -> Path:
    return edgar_dir(settings) / "manifest_universe.json"


def load_manifest(settings: Settings | None = None) -> dict[str, list[dict]]:
    """Load the universe manifest: {ticker: [filing rows]}. Empty if absent."""
    settings = settings or get_settings()
    path = manifest_path(settings)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_local_path(settings: Settings, manifest_row: dict) -> Path:
    """Absolute path of a manifest row's raw HTML file."""
    return project_root(settings) / manifest_row["local_path"]


def load_ticker_to_cik(settings: Settings | None = None) -> dict[str, int]:
    """Ticker -> CIK from SEC ``company_tickers.json`` (ported from notebook 01).

    The raw file is persisted to ``data/raw/edgar/company_tickers.json`` as an
    immutable input; the download happens only once.
    """
    settings = settings or get_settings()
    path = edgar_dir(settings) / "company_tickers.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("downloading %s", COMPANY_TICKERS_URL)
        path.write_bytes(_sec_get(COMPANY_TICKERS_URL, _identity(settings)))
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {row["ticker"]: int(row["cik_str"]) for row in raw.values()}


def _acquire_company(
    settings: Settings, ticker: str, annual_since: int
) -> list[dict]:
    """Download annuals (>= annual_since) + latest quarterly for one filer;
    return manifest rows (ported from notebook 12 acquire_company)."""
    import edgar  # heavy import kept local

    name, annual_form, quarterly_form = FILERS[ticker]
    company = edgar.Company(ticker)
    cdir = edgar_dir(settings) / ticker
    cdir.mkdir(parents=True, exist_ok=True)
    targets = [
        f
        for f in company.get_filings(form=annual_form)
        if f.filing_date.year >= annual_since
    ]
    if quarterly_form:
        targets.append(company.get_filings(form=quarterly_form).latest(1))
    root = project_root(settings)
    rows = []
    for f in targets:
        local = cdir / f"{f.form.replace('/', '-')}_{f.filing_date}_{f.accession_no}.html"
        if not local.exists():
            local.write_text(f.html(), encoding="utf-8")
            time.sleep(0.15)  # stay well under SEC's 10 req/s
        rows.append(
            {
                "ticker": ticker,
                "cik": f.cik,
                "form": f.form,
                "filing_date": str(f.filing_date),
                "accession_no": f.accession_no,
                "source_url": f.document.url,
                "local_path": str(local.resolve().relative_to(root)),
                "size_bytes": local.stat().st_size,
            }
        )
    logger.info("%s (%s): %d filings on disk", ticker, name, len(rows))
    return rows


def download_filings(
    settings: Settings | None = None,
    tickers: list[str] | None = None,
    *,
    annual_since: int = ANNUAL_SINCE,
) -> dict:
    """Acquire filings for the universe (idempotent; ported from notebook 12).

    A ticker already present in ``manifest_universe.json`` is skipped
    entirely (its files are on disk); the manifest is re-written after each
    newly acquired ticker so interruptions never lose completed work.

    Returns a summary dict:
    ``{"tickers": {ticker: {"filings": n, "cached": bool}},
       "total_filings": n, "manifest_path": str}``
    """
    settings = settings or get_settings()
    import edgar  # heavy import kept local

    edgar.set_identity(_identity(settings))
    edgar_dir(settings).mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(settings)
    tickers = list(tickers) if tickers else list(FILERS)
    unknown = [t for t in tickers if t not in FILERS]
    if unknown:
        raise KeyError(f"tickers not in FILERS universe: {unknown}")

    mpath = manifest_path(settings)
    summary_tickers: dict[str, dict] = {}
    for ticker in tickers:
        if ticker in manifest:
            logger.info("%s: %d filings (cached)", ticker, len(manifest[ticker]))
            summary_tickers[ticker] = {
                "filings": len(manifest[ticker]),
                "cached": True,
            }
            continue
        manifest[ticker] = _acquire_company(settings, ticker, annual_since)
        mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        summary_tickers[ticker] = {
            "filings": len(manifest[ticker]),
            "cached": False,
        }
    total = sum(len(rows) for t, rows in manifest.items() if t in summary_tickers)
    logger.info("total: %d filings across %d filers", total, len(summary_tickers))
    return {
        "tickers": summary_tickers,
        "total_filings": total,
        "manifest_path": str(mpath),
    }

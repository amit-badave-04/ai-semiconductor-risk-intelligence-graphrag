"""SEC EDGAR filing acquisition (ported from notebooks 01 and 12; incremental
per accession since M1).

Downloads each filer's annual reports plus the current fiscal year's
quarterly reports via edgartools, persisting raw HTML to::

    data/raw/edgar/<TICKER>/<FORM>_<filing_date>_<accession_no>.html

(``/`` in form names becomes ``-``, e.g. ``10-K/A`` -> ``10-K-A``) and
recording provenance in ``data/raw/edgar/manifest_universe.json`` — the
exact on-disk layout the notebooks created, so the pre-existing data lake
stays readable and already-downloaded filings are never re-fetched.

Target policy (one pure function, ``select_targets``, shared with
``freshness.pending_filings`` so "pending" always means "what the next
download would fetch"):

- every annual-form filing (amendments included, e.g. ``10-K/A``) filed in
  ``annual_since`` or later;
- every quarterly-form filing filed after the latest *original* annual
  filing (all current-fiscal-year quarters, not just the newest one);
- both bounded above by ``as_of`` (inclusive) when given.

The manifest is merged by ``accession_no``: rows already present are kept
verbatim (never rewritten, never dropped — even if they fall outside the
current rules), new rows are appended, and a ticker that gained rows is
ordered by ``(filing_date, accession_no)``. A ticker with nothing new is left
byte-for-byte untouched, so a no-op run never changes the manifest.

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
import os
import time
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..config import Settings, get_settings

logger = logging.getLogger("semigraph.ingestion.edgar")

from ..universe import ANNUAL_SINCE, FILERS  # noqa: E402,F401 — re-exported (single source: universe.py)

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

SEC_PAUSE_S = 0.15  # stay well under SEC's 10 req/s
SEC_HTTP_TIMEOUT_S = 60  # a stalled connection must fail (and be reported), not hang ingestion forever
_REPLACE_RETRIES = 3  # OneDrive/AV can briefly lock a file mid-replace on Windows


# ---------------------------------------------------------------- records

@dataclass(frozen=True)
class FilingRecord:
    """A filing as listed by an EDGAR source — lightweight and network-free.

    ``html`` fetches the primary document (called only when the raw file is
    missing). ``resolve_source_url`` lets a source defer an expensive URL
    lookup (edgartools' ``filing.document`` costs a request per filing) until
    the record is actually a download target.
    """

    ticker: str
    form: str
    filing_date: date
    accession_no: str
    cik: int
    source_url: str = ""
    period_of_report: str | None = None
    html: Callable[[], str] | None = field(default=None, compare=False, repr=False)
    resolve_source_url: Callable[[], str] | None = field(
        default=None, compare=False, repr=False
    )


Lister = Callable[[str, str], Sequence[FilingRecord]]


def parse_as_of(value: date | str | None) -> date | None:
    """Normalise an ``as_of`` bound (date, ISO string or None) to a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


# --------------------------------------------------------------- paths / IO

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
    return urllib.request.urlopen(req, timeout=SEC_HTTP_TIMEOUT_S).read()


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


def atomic_write(path: Path, write: Callable[[Path], None]) -> None:
    """``write(tmp)`` to a sibling temp file, then ``os.replace`` it over ``path``.

    A crash or exception mid-write never leaves a truncated ``path`` that later
    looks 'already done' (the previous file, if any, stays intact), and the temp
    file is always removed.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        write(tmp)
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.2)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """UTF-8 text via :func:`atomic_write` — a crash never leaves a truncated
    file that later looks 'already downloaded'."""
    atomic_write(path, lambda tmp: tmp.write_text(text, encoding="utf-8"))


# ------------------------------------------------------- selection (pure)

def _dedupe_by_accession(records: Iterable[FilingRecord]) -> list[FilingRecord]:
    seen: set[str] = set()
    unique: list[FilingRecord] = []
    for rec in records:
        if rec.accession_no not in seen:
            seen.add(rec.accession_no)
            unique.append(rec)
    return unique


def _quarterly_cutoff(
    annuals: Sequence[FilingRecord], annual_form: str, annual_since: int
) -> date:
    """Quarterlies must be filed strictly after this date.

    Anchored on the latest *original* annual filing (an amendment filed months
    later, e.g. a Part III 10-K/A, must not drop real 10-Qs). With no annual
    target at all, fall back to the start of the ``annual_since`` window.
    """
    originals = [r for r in annuals if r.form == annual_form] or list(annuals)
    if originals:
        return max(r.filing_date for r in originals)
    return date(annual_since - 1, 12, 31)


def select_targets(
    records: Iterable[FilingRecord],
    annual_form: str,
    quarterly_form: str | None,
    annual_since: int,
    as_of: date | None,
) -> list[FilingRecord]:
    """The filings the corpus should hold, sorted by (filing_date, accession).

    Filters on an explicit form set — ``get_filings(form='10-Q')`` also yields
    ``10-Q/A`` and ``get_filings(form='20-F')`` also yields ``20-F/A`` — so
    annual amendments are kept (as in v1) and quarterly amendments are not.
    """
    in_window = [r for r in records if as_of is None or r.filing_date <= as_of]
    annual_forms = {annual_form, f"{annual_form}/A"}
    annuals = [
        r for r in in_window
        if r.form in annual_forms and r.filing_date.year >= annual_since
    ]
    quarterlies: list[FilingRecord] = []
    if quarterly_form:
        cutoff = _quarterly_cutoff(annuals, annual_form, annual_since)
        quarterlies = [
            r for r in in_window
            if r.form == quarterly_form and r.filing_date > cutoff
        ]
    targets = _dedupe_by_accession([*annuals, *quarterlies])
    return sorted(targets, key=lambda r: (r.filing_date, r.accession_no))


def merge_manifest_rows(existing: Sequence[dict], new: Iterable[dict]) -> list[dict]:
    """Merge by ``accession_no``: existing rows verbatim, new rows appended,
    result ordered by ``(filing_date, accession_no)``. Inputs are not mutated."""
    known = {row["accession_no"] for row in existing}
    added: list[dict] = []
    for row in new:
        if row["accession_no"] not in known:
            known.add(row["accession_no"])
            added.append(row)
    return sorted(
        [*existing, *added], key=lambda r: (r["filing_date"], r["accession_no"])
    )


# ------------------------------------------------------- edgartools source

def _record_from_edgartools(ticker: str, filing) -> FilingRecord:
    """Wrap an edgartools ``EntityFiling`` without touching the network:
    ``filing.document`` (the URL) and ``filing.html()`` stay deferred, and the
    period comes from the stored ``report_date`` — never ``period_of_report``,
    which fetches the filing homepage per filing (and raises on old filings)."""
    period = getattr(filing, "report_date", None)
    return FilingRecord(
        ticker=ticker,
        form=str(filing.form),
        filing_date=parse_as_of(filing.filing_date),  # type: ignore[arg-type]
        accession_no=str(filing.accession_no),
        cik=int(filing.cik),
        period_of_report=str(period) if period else None,
        html=filing.html,
        resolve_source_url=lambda: filing.document.url,
    )


def _edgartools_lister(settings: Settings) -> Lister:
    """The real EDGAR source: ``Company.get_filings(form=...)`` per ticker."""
    import edgar  # heavy import kept local

    edgar.set_identity(_identity(settings))
    companies: dict[str, object] = {}

    def list_filings(ticker: str, form: str) -> list[FilingRecord]:
        if ticker not in companies:
            companies[ticker] = edgar.Company(ticker)
        company = companies[ticker]
        return [
            _record_from_edgartools(ticker, f)
            for f in company.get_filings(form=form)  # type: ignore[attr-defined]
        ]

    return list_filings


# ------------------------------------------------------------ acquisition

def _local_html_path(cdir: Path, rec: FilingRecord) -> Path:
    return cdir / f"{rec.form.replace('/', '-')}_{rec.filing_date.isoformat()}_{rec.accession_no}.html"


def _polite_sec_get(settings: Settings) -> Callable[[str], bytes]:
    """``fetch(url) -> bytes`` with the declared SEC identity and the fair-access pause."""
    identity = _identity(settings)

    def fetch(url: str) -> bytes:
        body = _sec_get(url, identity)
        time.sleep(SEC_PAUSE_S)
        return body

    return fetch


class FilingDownloadError(RuntimeError):
    """A filing's HTML could not be fetched (primary source and fallback both failed)."""


HtmlFallback = Callable[[FilingRecord], str]


def submissions_html_fallback(fetch: Callable[[str], bytes]) -> HtmlFallback:
    """Direct download of a filing's primary document, named by the submissions JSON.

    edgartools discovers the primary document by parsing the filing index page and
    returns None for some filings (INTC's 2026-07-24 10-Q); EDGAR's own submissions
    metadata (``primaryDocument``) is authoritative. The submissions document is
    fetched once per CIK. ``fetch(url) -> bytes`` is the network seam.
    """
    cache: dict[int, dict] = {}

    def primary_document(rec: FilingRecord) -> str:
        if rec.cik not in cache:
            cache[rec.cik] = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{rec.cik:010d}.json"))
        recent = cache[rec.cik].get("filings", {}).get("recent", {})
        names = dict(zip(recent.get("accessionNumber", []), recent.get("primaryDocument", []), strict=False))
        if not names.get(rec.accession_no):
            raise LookupError(f"{rec.accession_no}: not in the recent submissions of CIK {rec.cik}")
        url = (f"https://www.sec.gov/Archives/edgar/data/{rec.cik}/"
               f"{rec.accession_no.replace('-', '')}/{names[rec.accession_no]}")
        return fetch(url).decode("utf-8", errors="replace")

    return primary_document


def _has_content(html: object) -> bool:
    """True for a non-blank string. edgartools' ``filing.html()`` returns None for a
    PDF, binary or empty primary document — a failed fetch, not a filing."""
    return isinstance(html, str) and bool(html.strip())


def _fetch_html(rec: FilingRecord, fallback: HtmlFallback | None) -> str:
    """Primary source first (edgartools ``html()``), then the fallback; both errors are kept.

    A primary that raises, or returns None / blank text, counts as failed; a fallback
    that yields nothing is a :class:`FilingDownloadError`. Nothing empty is ever written.
    """
    if rec.html is None:
        raise FilingDownloadError(f"{rec.accession_no}: record has no html() source")
    cause: Exception | None = None
    try:
        html = rec.html()
    except Exception as primary:  # noqa: BLE001 — edgartools raises AttributeError, OSError, ...
        cause = primary
        primary_error = f"{type(primary).__name__}: {primary}"
    else:
        if _has_content(html):
            return html
        primary_error = f"html() returned no content ({'None' if html is None else repr(html)[:40]})"
    if fallback is None:
        raise FilingDownloadError(f"{rec.accession_no}: {primary_error}") from cause
    logger.warning("%s: primary %s — trying the submissions fallback", rec.accession_no, primary_error)
    try:
        html = fallback(rec)
    except Exception as second:  # noqa: BLE001
        raise FilingDownloadError(
            f"{rec.accession_no}: primary {primary_error}; fallback {type(second).__name__}: {second}"
        ) from second
    if not _has_content(html):
        raise FilingDownloadError(f"{rec.accession_no}: primary {primary_error}; fallback returned no content")
    return html


def _ensure_html(local: Path, rec: FilingRecord, fallback: HtmlFallback | None = None) -> None:
    """Download the raw HTML only when the file is missing (atomic write)."""
    if local.exists():
        return
    atomic_write_text(local, _fetch_html(rec, fallback))
    time.sleep(SEC_PAUSE_S)


def _filing_folder_url(rec: FilingRecord) -> str:
    """The EDGAR Archives folder that holds the filing (always derivable, always truthful)."""
    return f"https://www.sec.gov/Archives/edgar/data/{rec.cik}/{rec.accession_no.replace('-', '')}/"


def _source_url(rec: FilingRecord) -> str:
    """The filing's document URL, else its Archives folder.

    edgartools' ``filing.document`` is None exactly for the filings that needed the
    submissions fallback, so the deferred lookup can raise or come back empty; that
    must never cost the filing its manifest row.
    """
    if rec.source_url:
        return rec.source_url
    if rec.resolve_source_url is not None:
        try:
            url = rec.resolve_source_url()
        except Exception as e:  # noqa: BLE001 — AttributeError from filing.document.url, network errors, ...
            logger.warning("%s: source URL lookup failed (%s: %s) — using the filing folder",
                           rec.accession_no, type(e).__name__, e)
        else:
            if isinstance(url, str) and url:
                return url
            logger.warning("%s: no source URL available — using the filing folder", rec.accession_no)
    return _filing_folder_url(rec)


def _manifest_row(rec: FilingRecord, local: Path, root: Path) -> dict:
    source_url = _source_url(rec)
    return {
        "ticker": rec.ticker,
        "cik": rec.cik,
        "form": rec.form,
        "filing_date": rec.filing_date.isoformat(),
        "accession_no": rec.accession_no,
        "source_url": source_url,
        "local_path": str(local.resolve().relative_to(root)),
        "size_bytes": local.stat().st_size,
    }


def _acquire_new(
    settings: Settings, ticker: str, targets: Sequence[FilingRecord], known: set[str],
    fallback: HtmlFallback | None = None,
) -> tuple[list[dict], list[dict]]:
    """Download (if missing) and describe every target not yet in the manifest.

    Returns ``(rows, failures)``. Anything that goes wrong for one filing — fetch,
    write, or describing it (the manifest row is built inside the same guard) — is
    logged, listed in ``failures`` and skipped: it never aborts its siblings or later
    tickers, and leaves no manifest row. A file already written stays on disk, so the
    next run reuses it (no re-download) and only retries the row.
    """
    fresh = [r for r in targets if r.accession_no not in known]
    if not fresh:
        return [], []
    cdir = edgar_dir(settings) / ticker
    cdir.mkdir(parents=True, exist_ok=True)
    root = project_root(settings)
    rows: list[dict] = []
    failures: list[dict] = []
    for rec in fresh:
        local = _local_html_path(cdir, rec)
        try:
            _ensure_html(local, rec, fallback)
            rows.append(_manifest_row(rec, local, root))
        except Exception as e:  # noqa: BLE001 — per-filing isolation: one bad filing must not abort the run
            expected = isinstance(e, FilingDownloadError)
            error = str(e) if expected else f"{type(e).__name__}: {e}"
            logger.error("%s %s %s: not ingested — %s", ticker, rec.form, rec.filing_date, error,
                         exc_info=not expected)
            failures.append({"accession_no": rec.accession_no, "form": rec.form,
                             "filing_date": rec.filing_date.isoformat(), "error": error})
    return rows, failures


def _list_targets(
    lister: Lister, ticker: str, annual_since: int, as_of: date | None
) -> list[FilingRecord]:
    _, annual_form, quarterly_form = FILERS[ticker]
    records = list(lister(ticker, annual_form))
    if quarterly_form:
        records.extend(lister(ticker, quarterly_form))
    return select_targets(records, annual_form, quarterly_form, annual_since, as_of)


def _sync_ticker(
    settings: Settings,
    ticker: str,
    lister: Lister,
    annual_since: int,
    as_of: date | None,
    existing: Sequence[dict],
    fallback: HtmlFallback | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """One ticker: list targets, fetch the accessions not yet held, merge.

    Returns ``(merged manifest rows, newly acquired rows, failures)``."""
    targets = _list_targets(lister, ticker, annual_since, as_of)
    new_rows, failures = _acquire_new(
        settings, ticker, targets, {r["accession_no"] for r in existing}, fallback)
    return merge_manifest_rows(existing, new_rows), new_rows, failures


def download_filings(
    settings: Settings | None = None,
    tickers: list[str] | None = None,
    *,
    annual_since: int = ANNUAL_SINCE,
    as_of: date | str | None = None,
    lister: Lister | None = None,
    html_fallback: HtmlFallback | None = None,
) -> dict:
    """Incrementally acquire filings for the universe, per accession.

    For each ticker the target set (see module docstring) is listed, every
    accession not already in ``manifest_universe.json`` is downloaded (HTML
    only if its file is missing) and merged in; existing rows are never
    touched. The manifest is written atomically after each ticker so an
    interruption never loses completed work.

    ``lister(ticker, form)`` is the network seam (default: edgartools).
    ``html_fallback(record) -> html`` is tried when the primary ``html()`` raises
    (default with the real lister: :func:`submissions_html_fallback`). A filing
    that still cannot be fetched is skipped and reported, never fatal.

    Returns ``{"tickers": {ticker: {"filings": n, "new": [accession...],
    "cached": bool, "failed": [{accession_no, form, filing_date, error}]}},
    "total_filings": n, "failed_total": n, "manifest_path": str}`` where
    ``cached`` means nothing new was fetched for that ticker.
    """
    settings = settings or get_settings()
    tickers = list(tickers) if tickers else list(FILERS)
    unknown = [t for t in tickers if t not in FILERS]
    if unknown:
        raise KeyError(f"tickers not in FILERS universe: {unknown}")
    bound = parse_as_of(as_of)
    if lister is None:
        lister = _edgartools_lister(settings)
        html_fallback = html_fallback or submissions_html_fallback(_polite_sec_get(settings))
    edgar_dir(settings).mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(settings)
    mpath = manifest_path(settings)
    summary_tickers: dict[str, dict] = {}
    for ticker in tickers:
        existing = manifest.get(ticker, [])
        merged, new_rows, failures = _sync_ticker(
            settings, ticker, lister, annual_since, bound, existing, html_fallback)
        if new_rows:  # nothing new => the manifest bytes stay exactly as they were
            manifest = {**manifest, ticker: merged}
            atomic_write_text(mpath, json.dumps(manifest, indent=2))
        new_accessions = [r["accession_no"] for r in new_rows]
        logger.info(
            "%s (%s): %d filings, %d new", ticker, FILERS[ticker][0], len(merged), len(new_accessions)
        )
        summary_tickers[ticker] = {
            "filings": len(merged),
            "new": new_accessions,
            "cached": not new_accessions,
            "failed": failures,
        }
    total = sum(s["filings"] for s in summary_tickers.values())
    failed_total = sum(len(s["failed"]) for s in summary_tickers.values())
    logger.info("total: %d filings across %d filers (%d failed)", total, len(summary_tickers), failed_total)
    return {
        "tickers": summary_tickers,
        "total_filings": total,
        "failed_total": failed_total,
        "manifest_path": str(mpath),
    }

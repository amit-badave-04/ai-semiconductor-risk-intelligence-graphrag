"""Polite new-filing detection: what has appeared at the SEC / Federal Register
that the data lake does not hold yet?

- ``pending_filings`` reads each filer's SEC submissions JSON
  (``data.sec.gov/submissions/CIK##########.json``) and lists the in-scope
  filings whose accession is not in the manifest. "In scope" is exactly the
  download policy (``edgar.select_targets`` — one shared selector), so after a
  successful ``download_filings`` the pending list is empty.
- ``federal_register_pending`` compares the stored BIS rule count with a live
  count-only query.

Both take an injectable ``fetch(url) -> dict`` (tests never touch the network).
Real requests carry the declared ``SEC_USER_AGENT`` and sleep 0.15 s after each
call to stay well under the SEC's 10 requests/second.

The submissions JSON keeps parallel arrays under ``filings.recent`` (~1000
newest filings, form 4s included) and lists older pages under
``filings.files``; only ``recent`` is read here.
"""

import json
import logging
import time
import urllib.request
from collections.abc import Callable, Iterable
from datetime import date
from itertools import zip_longest

from ..config import Settings, get_settings
from ..universe import ANNUAL_SINCE, FILERS
from . import federal_register as fr
from .edgar import (
    FilingRecord,
    _identity,
    load_manifest,
    load_ticker_to_cik,
    parse_as_of,
    select_targets,
)

logger = logging.getLogger("semigraph.ingestion.freshness")

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_PAUSE_S = 0.15  # stay well under SEC's 10 req/s
HTTP_TIMEOUT_S = 30

Fetch = Callable[[str], dict]


def _http_get_json(url: str, user_agent: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_submissions(
    cik: int, *, fetch: Fetch | None = None, settings: Settings | None = None
) -> dict:
    """The SEC submissions JSON for one CIK.

    ``fetch(url)`` replaces the network call (no identity check, no sleep). The
    real path sends ``SEC_USER_AGENT`` and sleeps ``SEC_PAUSE_S`` afterwards.
    """
    url = SUBMISSIONS_URL.format(cik=cik)
    if fetch is not None:
        return fetch(url)
    settings = settings or get_settings()
    user_agent = _identity(settings)
    try:
        data = _http_get_json(url, user_agent)
    except OSError as e:
        raise RuntimeError(f"SEC submissions request failed for CIK {cik}: {e}") from e
    time.sleep(SEC_PAUSE_S)
    return data


def records_from_submissions(ticker: str, cik: int, submissions: dict) -> list[FilingRecord]:
    """Filings from ``filings.recent``'s parallel arrays (unparseable dates skipped)."""
    recent = ((submissions or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    periods = recent.get("reportDate") or []
    if not len(forms) == len(dates) == len(accessions):
        logger.warning(
            "%s: submissions arrays disagree in length (form=%d filingDate=%d accession=%d)",
            ticker, len(forms), len(dates), len(accessions),
        )
    records: list[FilingRecord] = []
    for form, day, accession, period in zip_longest(forms, dates, accessions, periods, fillvalue=""):
        if not (form and day and accession):
            continue
        try:
            filed = date.fromisoformat(day)
        except ValueError:
            logger.warning("%s: skipping %s — bad filingDate %r", ticker, accession, day)
            continue
        records.append(
            FilingRecord(
                ticker=ticker, form=form, filing_date=filed, accession_no=accession,
                cik=cik, period_of_report=period or None,
            )
        )
    return records


def _note_unread_pages(
    ticker: str, records: list[FilingRecord], submissions: dict, annual_form: str, annual_since: int
) -> None:
    """Older submission pages are not read. Say so — loudly only when the
    ``recent`` window shows no original annual filing and does not reach the
    ``annual_since`` window, i.e. when the pending list could be wrong."""
    pages = len(((submissions or {}).get("filings") or {}).get("files") or [])
    if not pages:
        return
    oldest = min((r.filing_date for r in records), default=None)
    sees_annual = any(r.form == annual_form for r in records)
    if not sees_annual and (oldest is None or oldest > date(annual_since, 1, 1)):
        logger.warning(
            "%s: %d older submission page(s) not read and the recent window (back to %s) shows no %s "
            "— pending filings may be incomplete", ticker, pages, oldest, annual_form,
        )
    else:
        logger.debug("%s: %d older submission page(s) not read (recent back to %s)", ticker, pages, oldest)


def _cik_resolver(settings: Settings, manifest: dict[str, list[dict]]) -> Callable[[str], int]:
    """ticker -> CIK from the manifest, else (lazily, once) ``company_tickers.json``."""
    fallback: dict[str, int] = {}

    def resolve(ticker: str) -> int:
        rows = manifest.get(ticker) or []
        if rows:
            return int(rows[0]["cik"])
        if not fallback:
            fallback.update(load_ticker_to_cik(settings))
        if ticker not in fallback:
            raise KeyError(f"no CIK known for {ticker!r} (not in manifest or company_tickers.json)")
        return fallback[ticker]

    return resolve


def _pending_row(rec: FilingRecord) -> dict:
    return {
        "ticker": rec.ticker,
        "cik": rec.cik,
        "form": rec.form,
        "filing_date": rec.filing_date.isoformat(),
        "accession_no": rec.accession_no,
        "period_of_report": rec.period_of_report,
    }


def pending_filings(
    settings: Settings | None = None,
    tickers: Iterable[str] | None = None,
    *,
    as_of: date | str | None = None,
    fetch: Fetch | None = None,
    annual_since: int = ANNUAL_SINCE,
) -> list[dict]:
    """In-scope filings on EDGAR that the manifest does not hold.

    In scope = ``edgar.select_targets`` (annual form and its ``/A`` amendments
    filed in ``annual_since`` or later, quarterly form filed after the latest
    original annual) with ``filing_date <= as_of``. Each row is ``{ticker, cik,
    form, filing_date, accession_no, period_of_report}``; rows follow the order
    of ``tickers`` then filing date.
    """
    settings = settings or get_settings()
    tickers = list(tickers) if tickers else list(FILERS)
    unknown = [t for t in tickers if t not in FILERS]
    if unknown:
        raise KeyError(f"tickers not in FILERS universe: {unknown}")
    bound = parse_as_of(as_of)
    manifest = load_manifest(settings)
    resolve_cik = _cik_resolver(settings, manifest)

    pending: list[dict] = []
    for ticker in tickers:
        _, annual_form, quarterly_form = FILERS[ticker]
        cik = resolve_cik(ticker)
        submissions = fetch_submissions(cik, fetch=fetch, settings=settings)
        records = records_from_submissions(ticker, cik, submissions)
        _note_unread_pages(ticker, records, submissions, annual_form, annual_since)
        known = {row["accession_no"] for row in manifest.get(ticker, [])}
        targets = select_targets(records, annual_form, quarterly_form, annual_since, bound)
        fresh = [_pending_row(r) for r in targets if r.accession_no not in known]
        logger.info("%s: %d pending filing(s)", ticker, len(fresh))
        pending.extend(fresh)
    return pending


def federal_register_pending(
    settings: Settings | None = None,
    *,
    as_of: date | str | None = None,
    fetch: Fetch | None = None,
) -> dict:
    """Stored BIS rules vs a live count-only query (same conditions, both
    bounded by ``publication_date <= as_of``).

    ``{"stored_count", "stored_latest_date", "live_count", "new_since_stored"}``
    — ``new_since_stored`` is ``max(0, live - stored)``. Any stored cache counts,
    legacy v1 files included (that is exactly what the graph would load).
    """
    settings = settings or get_settings()
    bound = parse_as_of(as_of)
    stored = fr.rules_published_by(fr.read_stored_rules(settings), bound)
    fetch = fetch if fetch is not None else fr.make_fetcher(settings)
    live_count = int(fetch(fr.count_query_url(bound))["count"])
    dates = [r["publication_date"] for r in stored if r.get("publication_date")]
    return {
        "stored_count": len(stored),
        "stored_latest_date": max(dates) if dates else None,
        "live_count": live_count,
        "new_since_stored": max(0, live_count - len(stored)),
    }

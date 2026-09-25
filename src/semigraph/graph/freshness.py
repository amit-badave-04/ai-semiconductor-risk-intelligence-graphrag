"""Filing freshness and evidence-span selection — pure, no Neo4j.

Which filing (and which SECTION of it) is current, what superseded what, and
which chunks become EvidenceSpans. The supersession RULES live in
``semigraph.versions``; this module turns their output into the validity
interval and retrievability that spans and risks inherit.

Validity: ``valid_from`` is the filing date; ``valid_to`` is ``VALID_TO_OPEN``
while the span is current, else the filing date of whatever ended it (the
superseding filing, or the amendment that restated the section). Edge cases
are explicit, never null:

- a corrected original superseded on the same day has ``valid_from == valid_to``;
- an inert filing (an unparsed amendment, or a form outside the annual/quarterly
  families) has an empty interval (``valid_to == valid_from``), is not current
  and not retrievable.

Retrievability (the default search filter) is decided PER SECTION: every section
of a current filing is retrievable, but of a superseded ANNUAL filing only its risk
section is (historical risk text — the history behind the lineages — stays
searchable; superseded Business / MD&A must not compete with the current filing).
Superseded quarterlies, corrected sections and inert filings are never retrievable.

Sections: annual filings are judged per (accession, section). A full
amendment retires its original as a whole; a PARTIAL amendment (AMD's 10-K/A
restates only Item 7) is an overlay — the original keeps every section the
amendment does not restate, and the restated ones move to the amendment.
"""

import logging
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace

import pandas as pd

from ..hashing import content_hash
from ..universe import FILERS, HIST_ANNUALS, RISK_SECTIONS
from ..versions import (
    AMENDMENT,
    CORRECTED,
    CURRENT,
    SUPERSEDED,
    FilingVersion,
    annual_periods,
    compute_filing_versions,
    current_quarterly_accession,
    effective_annual_sections,
)

logger = logging.getLogger("semigraph.graph.freshness")

VALID_TO_OPEN = "9999-12-31"  # valid_to sentinel while a span is current
SOURCE_TYPE_SEC = "sec_filing"
NVDA_TICKER = "NVDA"  # legacy PoC filer: every chunk is a span (see _span_scope)

ANNUAL, QUARTERLY, INERT = "annual", "quarterly", "inert"

Sections = Mapping[str, Collection[str]]  # accession -> section ids present in its chunks


@dataclass(frozen=True)
class FilingState:
    """A filing's version status plus what its spans and risks inherit."""

    accession_no: str
    form: str
    family: str                  # annual | quarterly | inert
    status: str                  # current | superseded | corrected | amendment
    supersede_kind: str | None   # rolled | corrected | None
    superseded_by: str | None
    is_current: bool
    valid_from: str              # the filing date (ISO)
    valid_to: str                # VALID_TO_OPEN while current, else the superseding filing's date
    # --- annual family only, when the sections of every filing are known ---
    owned_sections: frozenset[str] | None = None        # sections this filing is its period's effective source of
    corrected_sections: tuple[str, ...] = ()            # present in this filing but owned by nobody / a later filing
    restated: tuple[tuple[str, str], ...] = ()          # (corrected section, date it was restated)
    amends: str | None = None                           # overlay amendment: the filing it amends

    def owns(self, section_id: str) -> bool:
        """False only for an annual section that a later filing of the period restated."""
        return self.owned_sections is None or section_id in self.owned_sections

    def restated_on(self, section_id: str) -> str | None:
        return dict(self.restated).get(section_id)

    def retrievable_in(self, section_id: str) -> bool:
        """Filing-level part of default retrievability, for ONE section: everything of a current
        filing, and only the risk section (of the form's base form: 10-K/A -> 10-K) of a superseded
        annual filing. Ownership (a restated section) is applied on top by :func:`span_freshness`."""
        if self.is_current:
            return True
        return (self.status == SUPERSEDED and self.family == ANNUAL
                and section_id == RISK_SECTIONS.get(self.form.removesuffix("/A")))


@dataclass(frozen=True)
class SpanFreshness:
    status: str
    is_current: bool
    retrievable: bool
    valid_to: str


def span_freshness(state: FilingState, section_id: str) -> SpanFreshness:
    """Freshness of one span: status, currency and validity are filing-level, retrievability is
    per section (see :meth:`FilingState.retrievable_in`), and a section the period's amendment
    restated is ``corrected`` — neither current nor retrievable — and valid until the day it
    was restated."""
    if state.owns(section_id):
        return SpanFreshness(state.status, state.is_current, state.retrievable_in(section_id), state.valid_to)
    return SpanFreshness(CORRECTED, False, False, state.restated_on(section_id) or state.valid_to)


def versions_for_ticker(ticker: str, manifest_rows: Iterable[Mapping],
                        sections: Sections | None) -> list[FilingVersion]:
    """Filing versions of one filer. ``sections`` = accession -> section ids present in
    its chunks; its keys are the parsed accessions. ``None`` = unknown (legacy: every
    filing parsed, every amendment a whole-filing replacement)."""
    _, annual_form, quarterly_form = FILERS[ticker]
    return compute_filing_versions(
        manifest_rows, annual_form=annual_form, quarterly_form=quarterly_form,
        parsed=None if sections is None else set(sections), sections=sections)


def _inert_state(row: Mapping) -> FilingState:
    """A filing that plays no role: never current, never retrievable, empty validity interval."""
    return FilingState(row["accession_no"], row["form"], INERT, AMENDMENT, None, None, False,
                       row["filing_date"], row["filing_date"])


def _filing_level_state(version: FilingVersion, filing_dates: Mapping[str, str]) -> FilingState:
    superseder_date = filing_dates.get(version.superseded_by) if version.superseded_by else None
    valid_to = VALID_TO_OPEN if version.is_current else (superseder_date or version.filing_date)
    return FilingState(
        version.accession_no, version.form, version.family, version.status, version.supersede_kind,
        version.superseded_by, version.is_current, version.filing_date, valid_to)


def _section_owners(versions: list[FilingVersion], sections: Sections) -> dict[tuple[str, str], str]:
    """(accession, section) -> the later filing of the same period that restated it."""
    owners: dict[tuple[str, str], str] = {}
    for period in annual_periods(versions):  # oldest -> newest: the last filing with a section owns it
        latest = {s: acc for acc in period for s in sections.get(acc, ())}
        owners |= {(acc, s): latest[s] for acc in period for s in sections.get(acc, ()) if latest[s] != acc}
    return owners


def _overlay_base(version: FilingVersion, by_accession: Mapping[str, FilingVersion]) -> str | None:
    """The filing a PARTIAL amendment overlays (its period's effective filing), else None."""
    if version.family != ANNUAL or version.status not in (CURRENT, SUPERSEDED) or not version.period_key:
        return None
    original = by_accession.get(version.period_key)
    base = original.superseded_by if original and original.status == CORRECTED else version.period_key
    return None if base == version.accession_no else base


def _with_sections(state: FilingState, version: FilingVersion, sections: Sections,
                   owned: Mapping[str, frozenset[str]], owners: Mapping[tuple[str, str], str],
                   filing_dates: Mapping[str, str], by_accession: Mapping[str, FilingVersion]) -> FilingState:
    mine = frozenset(owned.get(version.accession_no, ()))
    corrected = tuple(sorted(set(sections.get(version.accession_no, ())) - mine))
    restated = tuple((s, filing_dates.get(owners.get((version.accession_no, s)), state.valid_to))
                     for s in corrected)
    return replace(state, owned_sections=mine, corrected_sections=corrected, restated=restated,
                   amends=_overlay_base(version, by_accession))


def derive_filing_states(manifest_rows: Iterable[Mapping], versions: Iterable[FilingVersion],
                         sections: Sections | None = None) -> dict[str, FilingState]:
    """accession -> FilingState for EVERY manifest row.

    ``sections`` (accession -> section ids in its chunks) makes annual states
    section-aware (owned / corrected sections, overlay amendments); without it
    annual filings are judged as a whole. Filings outside the annual/quarterly
    families (e.g. a 10-Q/A) get an inert state rather than vanishing, so
    nothing downstream ever meets a filing without a validity interval.
    """
    rows, versions = list(manifest_rows), list(versions)
    by_accession = {v.accession_no: v for v in versions}
    filing_dates = {r["accession_no"]: r["filing_date"] for r in rows}
    unversioned = [r["accession_no"] for r in rows if r["accession_no"] not in by_accession]
    if unversioned:
        logger.warning("%d filing(s) outside the annual/quarterly families treated as inert: %s",
                       len(unversioned), unversioned)
    owned = effective_annual_sections(versions, sections) if sections is not None else {}
    owners = _section_owners(versions, sections) if sections is not None else {}
    states: dict[str, FilingState] = {}
    for row in rows:
        version = by_accession.get(row["accession_no"])
        if version is None:
            states[row["accession_no"]] = _inert_state(row)
            continue
        state = _filing_level_state(version, filing_dates)
        if sections is not None and version.family == ANNUAL:
            state = _with_sections(state, version, sections, owned, owners, filing_dates, by_accession)
        states[row["accession_no"]] = state
    return states


def current_filings_without_chunks(states: Mapping[str, FilingState],
                                   chunk_accessions: Collection[str]) -> list[str]:
    """Current filings that have no chunks yet: nothing current can be retrieved from
    them, and the filing they replaced may already have left the default results."""
    return sorted(a for a, s in states.items() if s.is_current and a not in chunk_accessions)


def build_filing_rows(manifest_rows: Iterable[Mapping], states: Mapping[str, FilingState]) -> list[dict]:
    return [{"ticker": r["ticker"], "accession_no": r["accession_no"], "form": r["form"],
             "filing_date": r["filing_date"], "source_url": r.get("source_url"),
             "status": states[r["accession_no"]].status,
             "supersede_kind": states[r["accession_no"]].supersede_kind,
             "superseded_by": states[r["accession_no"]].superseded_by,
             "is_current": states[r["accession_no"]].is_current,
             "corrected_sections": list(states[r["accession_no"]].corrected_sections)}
            for r in manifest_rows]


def build_supersedes_rows(states: Mapping[str, FilingState]) -> list[dict]:
    """One ``(newer)-[:SUPERSEDES {kind}]->(older)`` row per superseded filing."""
    return sorted(({"newer": s.superseded_by, "older": s.accession_no, "kind": s.supersede_kind}
                   for s in states.values() if s.superseded_by),
                  key=lambda e: (e["older"], e["newer"]))


def build_amends_rows(states: Mapping[str, FilingState]) -> list[dict]:
    """One ``(amendment)-[:AMENDS {sections}]->(original)`` row per partial-amendment overlay."""
    return sorted(({"newer": s.accession_no, "older": s.amends, "sections": sorted(s.owned_sections or ())}
                   for s in states.values() if s.amends),
                  key=lambda e: (e["newer"], e["older"]))


def risk_stamp(state: FilingState, filer_cik: int, section_id: str) -> dict:
    """RiskFactor freshness props: a risk inherits the freshness of its evidence span."""
    fresh = span_freshness(state, section_id)
    return {"filer_cik": int(filer_cik), "form": state.form, "filing_date": state.valid_from,
            "valid_from": state.valid_from, "valid_to": fresh.valid_to, "is_current": fresh.is_current}


def chunk_sections(ch: pd.DataFrame) -> dict[str, set[str]]:
    """accession -> section ids present in the chunk frame."""
    if ch.empty:
        return {}
    return {acc: set(sec) for acc, sec in ch.groupby("accession_no")["section_id"]}


def _filing_rows(ch: pd.DataFrame) -> list[dict]:
    filings = ch[["accession_no", "form", "filing_date"]].drop_duplicates("accession_no")
    return [{"accession_no": r.accession_no, "form": r.form, "filing_date": str(r.filing_date)[:10]}
            for r in filings.itertuples()]


def _chunk_ids(ch: pd.DataFrame, accession: str, allowed: Collection[str] | None = None) -> set[str]:
    mask = ch["accession_no"] == accession
    if allowed is not None:
        mask &= ch["section_id"].isin(allowed)
    return set(ch.loc[mask, "chunk_id"])


def _span_scope(ch: pd.DataFrame, ticker: str, hist_annuals: int = HIST_ANNUALS,
                versions: list[FilingVersion] | None = None) -> pd.DataFrame:
    """The CURRENT extraction scope, in chunk-parquet order: the latest annual period
    (every section its filings own) + the current quarterly + ``hist_annuals``
    prior annual periods (risk sections only — the history behind the lineages).

    Mirrors ``extractor.extraction_scope``. "Latest" comes from
    ``semigraph.versions`` (filing date, never the accession string); a full
    10-K/A stands in for its original, a partial one overlays only the sections
    it restates; a quarterly retired by a newer annual is out of scope. This
    scope only decides what is loaded WITHOUT an extraction record —
    ``select_span_chunks`` unions it with every already-extracted chunk, so
    history is never dropped.

    NVDA is the exception: notebook 09 loaded ALL PoC chunks as spans before
    notebook 12 existed, and the proven graph (and NVDA's embedding cache)
    contains all of them — so NVDA returns the full frame.
    """
    if ticker == NVDA_TICKER:
        return ch
    if ch.empty or "form" not in ch.columns:
        return ch.iloc[0:0]
    sections = chunk_sections(ch)
    versions = versions if versions is not None else versions_for_ticker(ticker, _filing_rows(ch), sections)
    owned = effective_annual_sections(versions, sections)
    periods = annual_periods(versions, only=set(sections))
    _, annual_form, _ = FILERS[ticker]
    keep: set[str] = set()
    if periods:
        for accession in periods[-1]:                                      # latest period: everything effective
            keep |= _chunk_ids(ch, accession, owned.get(accession, ()))
        risk = {RISK_SECTIONS[annual_form]}
        for period in periods[-(1 + hist_annuals):-1]:                      # history: risks only
            for accession in period:
                keep |= _chunk_ids(ch, accession, risk & owned.get(accession, frozenset()))
    quarterly = current_quarterly_accession(versions)
    if quarterly in sections:
        keep |= _chunk_ids(ch, quarterly)
    return ch[ch["chunk_id"].isin(keep)]  # parquet order, cache-compatible


def select_span_chunks(ch: pd.DataFrame, ticker: str, extracted_ids: Iterable[str],
                       versions: list[FilingVersion] | None = None,
                       hist_annuals: int = HIST_ANNUALS) -> pd.DataFrame:
    """Chunks that become EvidenceSpans: every chunk with an extraction record
    UNION the current extraction scope, in chunk-parquet order.

    Row order is preserved so ``Embedder.encode_chunks_cached`` recognizes the
    notebooks' caches.
    """
    if ch.empty:
        return ch.iloc[0:0]
    keep = set(_span_scope(ch, ticker, hist_annuals, versions)["chunk_id"]) | set(extracted_ids)
    return ch[ch["chunk_id"].isin(keep)]


def build_span_rows(spans: pd.DataFrame, vectors, states: Mapping[str, FilingState],
                    mentioned: Callable[[str], list[int]]) -> list[dict]:
    """One EvidenceSpan row per chunk with all contract properties (dates as ISO strings)."""
    missing = sorted(set(spans["accession_no"]) - set(states))
    if missing:
        raise ValueError(f"chunks of filing(s) {missing} have no manifest row — "
                         "re-run the acquisition stage so every chunked filing is in the manifest")
    rows = []
    for r, vec in zip(spans.itertuples(), vectors):
        state = states[r.accession_no]
        fresh = span_freshness(state, r.section_id)
        rows.append({
            "chunk_id": r.chunk_id, "text": r.text, "kind": r.kind, "sub_heading": r.sub_heading,
            "section_key": f"{r.accession_no}:{r.section_id}",
            "char_start": int(r.char_start), "char_end": int(r.char_end), "n_tokens": int(r.n_tokens),
            "source_url": r.source_url, "embedding": [float(x) for x in vec],
            "mentions": mentioned(r.text),
            "content_hash": content_hash(r.text), "filer_cik": int(r.cik), "form": r.form,
            "accession_no": r.accession_no, "section_id": r.section_id,
            "filing_date": state.valid_from, "valid_from": state.valid_from, "valid_to": fresh.valid_to,
            "is_current": fresh.is_current, "retrievable": fresh.retrievable, "status": fresh.status,
            "source_type": SOURCE_TYPE_SEC,
        })
    return rows

"""Filing versions — which filing is CURRENT and what superseded what.

Pure, deterministic, no LLM. Two axes are deliberately kept apart:

- this module decides which filing VERSION (and which of its SECTIONS) is
  current; it is document versioning;
- ``graph.temporal`` decides whether a risk LINEAGE is Active or Deleted
  (fact validity).

Rules (per company, per annual/quarterly form family):

- **Rolled**: a newer period replaces an older one. The older filing stays
  valid for its own period (as-of queries) and is labelled "prior period".
  A new annual also retires every quarterly filed before it; a newer
  quarterly retires the previous one.
- **Corrected — whole filing**: a parsed amendment (10-K/A ...) that contains
  every section of its original replaces it as the period's effective filing;
  the original is marked ``corrected``.
- **Corrected — sections only**: a parsed amendment that contains only SOME
  sections (AMD's 10-K/A of 2026-02-04 restates Item 7 alone) is an overlay:
  the original stays the period's filing and only the sections the amendment
  contains are taken from the amendment. Business and Risk Factors are NOT
  retired by a MD&A-only correction.
- An amendment whose content was not parsed (Part III only, unsegmentable) does
  not replace anything and is marked ``amendment``.
- ``sections=None`` (section contents unknown) treats every parsed amendment as
  a whole-filing replacement — the conservative legacy default; pass the
  section ids per accession whenever they are known.
- Order is ``(filing_date, accession_no)``: same-day filings (AMD's 10-K and
  10-K/A) are ordered by accession number, which is sequential per filer.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

CURRENT = "current"
SUPERSEDED = "superseded"
CORRECTED = "corrected"
AMENDMENT = "amendment"

ROLLED = "rolled"
CORRECTION = "corrected"

_REQUIRED = ("accession_no", "form", "filing_date")

_State = tuple[str, str | None, str | None]  # (status, supersede_kind, superseded_by)


@dataclass(frozen=True)
class FilingVersion:
    accession_no: str
    form: str
    filing_date: str
    family: str  # "annual" | "quarterly"
    status: str  # current | superseded | corrected | amendment
    supersede_kind: str | None  # "rolled" | "corrected" | None
    superseded_by: str | None
    period_key: str | None = None  # annual family: accession of the period's original filing

    @property
    def is_current(self) -> bool:
        return self.status == CURRENT


def _key(row: Mapping) -> tuple[str, str]:
    return (row["filing_date"], row["accession_no"])


def _validated(filings: Iterable[Mapping]) -> list[Mapping]:
    rows = []
    for row in filings:
        missing = [k for k in _REQUIRED if not row.get(k)]
        if missing:
            raise ValueError(f"filing row is missing {missing}: {dict(row)}")
        rows.append(row)
    return rows


def _last_original_before(originals: list[Mapping], amendment: Mapping) -> Mapping | None:
    earlier = [o for o in originals if _key(o) <= _key(amendment)]
    return earlier[-1] if earlier else None


def _replaces_whole_filing(original: Mapping, amendment: Mapping,
                           sections: Mapping[str, Collection[str]] | None) -> bool:
    """True when the amendment contains every section its original has (or sections are unknown)."""
    if sections is None:
        return True
    original_sections = set(sections.get(original["accession_no"], ()))
    amendment_sections = set(sections.get(amendment["accession_no"], ()))
    return bool(amendment_sections) and original_sections <= amendment_sections


@dataclass(frozen=True)
class _Period:
    original: Mapping
    effective: str                 # accession that stands for the period (original or a full amendment)
    overlays: tuple[Mapping, ...]  # parsed PARTIAL amendments layered over the effective filing
    amendments: tuple[Mapping, ...]  # every amendment attached to this period


def _build_periods(originals: list[Mapping], amendments: list[Mapping], is_parsed,
                   sections: Mapping[str, Collection[str]] | None) -> list[_Period]:
    attached: dict[str, list[Mapping]] = {o["accession_no"]: [] for o in originals}
    for amendment in amendments:
        target = _last_original_before(originals, amendment)
        if target is not None:
            attached[target["accession_no"]].append(amendment)
    periods = []
    for o in originals:
        mine = attached[o["accession_no"]]
        parsed = [a for a in mine if is_parsed(a["accession_no"])]
        full = [a for a in parsed if _replaces_whole_filing(o, a, sections)]
        effective = full[-1]["accession_no"] if full else o["accession_no"]
        overlays = tuple(a for a in parsed if a not in full)
        periods.append(_Period(o, effective, overlays, tuple(mine)))
    return periods


def _annual_states(periods: list[_Period], amendments: list[Mapping]) -> dict[str, _State]:
    """accession -> (status, kind, superseded_by) for the annual family."""
    states: dict[str, _State] = {}
    for i, period in enumerate(periods):
        acc, eff = period.original["accession_no"], period.effective
        following = periods[i + 1].effective if i + 1 < len(periods) else None
        state: _State = (CURRENT, None, None) if following is None else (SUPERSEDED, ROLLED, following)
        if eff != acc:
            states[acc] = (CORRECTED, CORRECTION, eff)
        states[eff] = state
        for overlay in period.overlays:  # a partial amendment shares its period's fate
            states[overlay["accession_no"]] = state
    for amendment in amendments:
        states.setdefault(amendment["accession_no"], (AMENDMENT, None, None))
    return states


def _quarterly_states(quarterlies: list[Mapping], periods: list[_Period]) -> dict[str, _State]:
    """A quarterly is current only while nothing later (a newer 10-Q or the next annual) exists."""
    later = [(_key(q), q["accession_no"]) for q in quarterlies]
    later += [(_key(p.original), p.effective) for p in periods]
    states: dict[str, _State] = {}
    for q in quarterlies:
        following = sorted(c for c in later if c[0] > _key(q))
        states[q["accession_no"]] = ((CURRENT, None, None) if not following
                                     else (SUPERSEDED, ROLLED, following[0][1]))
    return states


def compute_filing_versions(filings: Iterable[Mapping], *, annual_form: str,
                            quarterly_form: str | None,
                            parsed: set[str] | None = None,
                            sections: Mapping[str, Collection[str]] | None = None) -> list[FilingVersion]:
    """Version status for every annual/quarterly/amendment filing of ONE company.

    ``parsed`` is the set of accessions whose content is available (has chunks);
    ``None`` means "assume all". ``sections`` maps accession -> section ids present
    in its content; it decides whether an amendment replaces its original as a whole
    or only overlays the sections it contains (see module docstring). Forms other
    than the three families (8-K, 10-Q/A ...) are ignored. The result is sorted by
    ``(filing_date, accession_no)``.
    """
    amend_form = f"{annual_form}/A"
    rows = sorted(_validated(filings), key=_key)
    originals = [r for r in rows if r["form"] == annual_form]
    amendments = [r for r in rows if r["form"] == amend_form]
    quarterlies = [r for r in rows if quarterly_form and r["form"] == quarterly_form]

    def is_parsed(accession: str) -> bool:
        return parsed is None or accession in parsed

    periods = _build_periods(originals, amendments, is_parsed, sections)
    states = _annual_states(periods, amendments)
    states |= _quarterly_states(quarterlies, periods)
    period_of = {a["accession_no"]: p.original["accession_no"] for p in periods for a in p.amendments}
    period_of |= {p.original["accession_no"]: p.original["accession_no"] for p in periods}

    return [
        FilingVersion(
            accession_no=r["accession_no"], form=r["form"], filing_date=r["filing_date"],
            family="annual" if r["form"] in (annual_form, amend_form) else "quarterly",
            status=states[r["accession_no"]][0], supersede_kind=states[r["accession_no"]][1],
            superseded_by=states[r["accession_no"]][2], period_key=period_of.get(r["accession_no"]),
        )
        for r in rows if r["accession_no"] in states
    ]


def annual_effective_accessions(versions: Iterable[FilingVersion],
                                only: set[str] | None = None) -> list[str]:
    """Effective annual filings in period order (a full amendment stands in for its original).

    Partial-amendment overlays are included too, next to their original; use
    :func:`annual_periods` to keep the grouping. ``only`` restricts the result to
    accessions that have content (e.g. chunks), so unsegmentable filings never
    occupy a history slot.
    """
    eff = [v for v in versions if v.family == "annual" and v.status in (CURRENT, SUPERSEDED)]
    eff = sorted(eff, key=lambda v: (v.filing_date, v.accession_no))
    return [v.accession_no for v in eff if only is None or v.accession_no in only]


def annual_periods(versions: Iterable[FilingVersion],
                   only: set[str] | None = None) -> list[tuple[str, ...]]:
    """Annual periods oldest -> newest; each period lists its effective filings
    (original first, then any overlay amendment)."""
    members: dict[str, list[FilingVersion]] = {}
    for v in versions:
        if v.family == "annual" and v.status in (CURRENT, SUPERSEDED) and v.period_key:
            if only is None or v.accession_no in only:
                members.setdefault(v.period_key, []).append(v)
    ordered = [sorted(vs, key=lambda v: (v.filing_date, v.accession_no)) for vs in members.values()]
    ordered.sort(key=lambda vs: (vs[0].filing_date, vs[0].accession_no))
    return [tuple(v.accession_no for v in vs) for vs in ordered]


def effective_annual_sections(versions: Iterable[FilingVersion],
                              sections: Mapping[str, Collection[str]]) -> dict[str, frozenset[str]]:
    """accession -> the sections for which that filing is its period's effective source.

    Within a period the LAST filing (by date, accession) that contains a section
    owns it: AMD's original 10-K owns Business + Risk Factors, its MD&A-only 10-K/A
    owns Item 7. A filing that owns nothing is omitted.
    """
    versions = list(versions)
    owned: dict[str, set[str]] = {}
    for period in annual_periods(versions):
        owner: dict[str, str] = {}
        for accession in period:                       # oldest -> newest: later filings win
            for section in sections.get(accession, ()):
                owner[section] = accession
        for section, accession in owner.items():
            owned.setdefault(accession, set()).add(section)
    return {accession: frozenset(s) for accession, s in owned.items()}


def current_quarterly_accession(versions: Iterable[FilingVersion]) -> str | None:
    """The quarterly filing that is still current, or None (e.g. a new annual just landed)."""
    current = [v for v in versions if v.family == "quarterly" and v.is_current]
    return current[-1].accession_no if current else None

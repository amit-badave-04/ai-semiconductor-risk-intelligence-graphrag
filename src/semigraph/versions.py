"""Filing versions — which filing is CURRENT and what superseded what.

Pure, deterministic, no LLM. Two axes are deliberately kept apart:

- this module decides which filing VERSION is current (document versioning);
- ``graph.temporal`` decides whether a risk LINEAGE is Active or Deleted
  (fact validity).

Rules (per company, per annual/quarterly form family):

- **Rolled**: a newer period replaces an older one. The older filing stays
  valid for its own period (as-of queries) and is labelled "prior period".
  A new annual also retires every quarterly filed before it; a newer
  quarterly retires the previous one.
- **Corrected**: a parsed amendment (10-K/A ...) replaces its original as the
  period's effective filing; the original is marked ``corrected``. An
  amendment whose content was not parsed (Part III only, unsegmentable) does
  NOT replace anything and is marked ``amendment``.
- Order is ``(filing_date, accession_no)``: same-day filings (AMD's 10-K and
  10-K/A) are ordered by accession number, which is sequential per filer.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

CURRENT = "current"
SUPERSEDED = "superseded"
CORRECTED = "corrected"
AMENDMENT = "amendment"

ROLLED = "rolled"
CORRECTION = "corrected"

_REQUIRED = ("accession_no", "form", "filing_date")


@dataclass(frozen=True)
class FilingVersion:
    accession_no: str
    form: str
    filing_date: str
    family: str  # "annual" | "quarterly"
    status: str  # current | superseded | corrected | amendment
    supersede_kind: str | None  # "rolled" | "corrected" | None
    superseded_by: str | None

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


def _annual_states(originals: list[Mapping], amendments: list[Mapping],
                   is_parsed) -> dict[str, tuple[str, str | None, str | None]]:
    """accession -> (status, kind, superseded_by) for the annual family."""
    amends_of: dict[str, list[Mapping]] = {o["accession_no"]: [] for o in originals}
    for amendment in amendments:
        target = _last_original_before(originals, amendment)
        if target is not None:
            amends_of[target["accession_no"]].append(amendment)
    effective = {}
    for o in originals:
        parsed = [a for a in amends_of[o["accession_no"]] if is_parsed(a["accession_no"])]
        effective[o["accession_no"]] = parsed[-1]["accession_no"] if parsed else o["accession_no"]

    states: dict[str, tuple[str, str | None, str | None]] = {}
    for i, o in enumerate(originals):
        acc, eff = o["accession_no"], effective[o["accession_no"]]
        if eff != acc:
            states[acc] = (CORRECTED, CORRECTION, eff)
        following = effective[originals[i + 1]["accession_no"]] if i + 1 < len(originals) else None
        states[eff] = (CURRENT, None, None) if following is None else (SUPERSEDED, ROLLED, following)
    for amendment in amendments:
        states.setdefault(amendment["accession_no"], (AMENDMENT, None, None))
    return states


def _quarterly_states(quarterlies: list[Mapping], originals: list[Mapping],
                      effective_annual: dict[str, str]) -> dict[str, tuple[str, str | None, str | None]]:
    """A quarterly is current only while nothing later (a newer 10-Q or the next annual) exists."""
    later = [(_key(q), q["accession_no"]) for q in quarterlies]
    later += [(_key(o), effective_annual[o["accession_no"]]) for o in originals]
    states = {}
    for q in quarterlies:
        following = sorted(c for c in later if c[0] > _key(q))
        states[q["accession_no"]] = ((CURRENT, None, None) if not following
                                     else (SUPERSEDED, ROLLED, following[0][1]))
    return states


def compute_filing_versions(filings: Iterable[Mapping], *, annual_form: str,
                            quarterly_form: str | None,
                            parsed: set[str] | None = None) -> list[FilingVersion]:
    """Version status for every annual/quarterly/amendment filing of ONE company.

    ``parsed`` is the set of accessions whose content is available (has chunks);
    ``None`` means "assume all". Forms other than the three families (8-K,
    10-Q/A ...) are ignored. The result is sorted by ``(filing_date, accession_no)``.
    """
    amend_form = f"{annual_form}/A"
    rows = sorted(_validated(filings), key=_key)
    originals = [r for r in rows if r["form"] == annual_form]
    amendments = [r for r in rows if r["form"] == amend_form]
    quarterlies = [r for r in rows if quarterly_form and r["form"] == quarterly_form]

    def is_parsed(accession: str) -> bool:
        return parsed is None or accession in parsed

    states = _annual_states(originals, amendments, is_parsed)
    effective_annual = {o["accession_no"]: _effective_of(o["accession_no"], states) for o in originals}
    states |= _quarterly_states(quarterlies, originals, effective_annual)

    known = [r for r in rows if r["accession_no"] in states]
    return [
        FilingVersion(
            accession_no=r["accession_no"], form=r["form"], filing_date=r["filing_date"],
            family="annual" if r["form"] in (annual_form, amend_form) else "quarterly",
            status=states[r["accession_no"]][0], supersede_kind=states[r["accession_no"]][1],
            superseded_by=states[r["accession_no"]][2],
        )
        for r in known
    ]


def _effective_of(original_acc: str, states: dict) -> str:
    """The accession that stands for this original's period (itself, or its parsed amendment)."""
    status, _, superseded_by = states[original_acc]
    return superseded_by if status == CORRECTED else original_acc


def annual_effective_accessions(versions: Iterable[FilingVersion],
                                only: set[str] | None = None) -> list[str]:
    """Effective annual filings in period order (an amendment stands in for its original).

    ``only`` restricts the result to accessions that have content (e.g. chunks),
    so unsegmentable filings never occupy a history slot.
    """
    eff = [v for v in versions if v.family == "annual" and v.status in (CURRENT, SUPERSEDED)]
    eff = sorted(eff, key=lambda v: (v.filing_date, v.accession_no))
    return [v.accession_no for v in eff if only is None or v.accession_no in only]


def current_quarterly_accession(versions: Iterable[FilingVersion]) -> str | None:
    """The quarterly filing that is still current, or None (e.g. a new annual just landed)."""
    current = [v for v in versions if v.family == "quarterly" and v.is_current]
    return current[-1].accession_no if current else None

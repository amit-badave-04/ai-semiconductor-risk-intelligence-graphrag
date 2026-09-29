"""Upload workspace version bookkeeping (M4, docs/v2/M4_PLAN.md 4.2, 5, 14.4).

Pure, deterministic, no I/O and no Neo4j — the same spirit as the top-level :mod:`semigraph.versions` (filing versions),
whose ``CURRENT`` / ``SUPERSEDED`` status vocabulary :mod:`semigraph.uploads.repo` reuses for ``UserVersion`` /
``UserChunk`` rows rather than retyping the strings here.

``as_of`` semantics (docs/v2/M4_PLAN.md 14.4, revision 3): a date ``D`` means the workspace as it stood at the END of
``D`` (UTC). The cutoff is ``D + 1 day`` at ``00:00 UTC``; a version or chunk is visible iff
``valid_from < cutoff AND valid_to >= cutoff``. Current rows carry ``valid_to = CURRENT_VALID_TO``. Every timestamp that
crosses into Neo4j (``valid_from``, ``valid_to``, ``cutoff``, ``now``) must be a timezone-aware ``datetime`` — an ISO
*string* silently fails the driver's range comparison against a stored ``DateTime`` property (no error, just an empty
result), so :mod:`semigraph.uploads.repo` never passes one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

#: ``valid_to`` of a row that is still current — a real (far-future) zoned datetime, never a sentinel string, so the
#: SEARCH range filter (``valid_to >= $cutoff``) works on it exactly like any superseded row's real ``valid_to``.
CURRENT_VALID_TO = datetime(9999, 12, 31, tzinfo=UTC)


def next_version(existing: list[int]) -> int:
    """The next version ordinal for a document: ``1`` for a brand-new document, else one past the highest existing."""
    return max(existing, default=0) + 1


def as_of_cutoff(as_of: str) -> datetime:
    """The cutoff instant for ``as_of`` (docs/v2/M4_PLAN.md 15.1): a ``YYYY-MM-DD`` date's cutoff is the end of that
    UTC day (``D + 1 day`` at ``00:00 UTC``); an ISO instant's cutoff is that instant plus one microsecond, so a
    version created AT EXACTLY ``as_of`` (the page's own "ask as of vN" using that version's ``created_at``) is
    still visible under ``valid_from < cutoff``.

    ``as_of`` is already validated and normalized by :func:`semigraph.serve.guard.validate_as_of` — a date is never
    ambiguous with an instant because only an instant contains ``T``.
    """
    if "T" in as_of:
        instant = datetime.fromisoformat(as_of)
        if instant.tzinfo is None:
            raise ValueError(f"as_of instant must carry a UTC offset, got {as_of!r}")
        return instant.astimezone(UTC) + timedelta(microseconds=1)
    day = date.fromisoformat(as_of)
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)


def is_visible(valid_from: datetime, valid_to: datetime, cutoff: datetime) -> bool:
    """True when a version/chunk row was in effect at ``cutoff`` (the Python mirror of the Cypher ``SEARCH ... WHERE``
    range filter :func:`semigraph.uploads.repo.search_chunks` runs with an ``as_of`` cutoff)."""
    return valid_from < cutoff and valid_to >= cutoff

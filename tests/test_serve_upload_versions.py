"""``uploads.versions``: pure version bookkeeping for upload workspaces (M4, docs/v2/M4_PLAN.md 4.2, 14.4, 15.1)."""

from datetime import UTC, datetime, timedelta

import pytest

from semigraph.serve import guard
from semigraph.uploads import versions


def test_next_version_of_a_brand_new_document_is_one():
    assert versions.next_version([]) == 1


@pytest.mark.parametrize("existing,expected", [([1], 2), ([1, 2, 3], 4), ([1, 3], 4), ([5], 6)],
                         ids=["one-existing", "sequential", "gap", "high-start"])
def test_next_version_is_one_past_the_highest_existing(existing, expected):
    assert versions.next_version(existing) == expected


def test_as_of_cutoff_is_the_start_of_the_next_utc_day():
    cutoff = versions.as_of_cutoff("2026-09-24")
    assert cutoff == datetime(2026, 9, 25, 0, 0, 0, tzinfo=UTC)


def test_as_of_cutoff_is_timezone_aware():
    cutoff = versions.as_of_cutoff("2026-01-01")
    assert cutoff.tzinfo is not None


# ---------------------------------------------------------------------- as_of_cutoff: instant form (finding 10, 15.1)

def test_as_of_cutoff_of_an_instant_is_that_instant_plus_one_microsecond():
    cutoff = versions.as_of_cutoff("2026-09-24T12:34:56.123456+00:00")
    assert cutoff == datetime(2026, 9, 24, 12, 34, 56, 123457, tzinfo=UTC)


def test_as_of_cutoff_of_an_instant_normalizes_a_non_utc_offset():
    # 09:00+05:00 is 04:00 UTC; the cutoff must be expressed in UTC either way.
    cutoff = versions.as_of_cutoff("2026-09-24T09:00:00+05:00")
    assert cutoff == datetime(2026, 9, 24, 4, 0, 0, 1, tzinfo=UTC)


def test_as_of_cutoff_of_a_z_suffixed_instant():
    cutoff = versions.as_of_cutoff("2026-09-24T00:00:00Z")
    assert cutoff == datetime(2026, 9, 24, 0, 0, 0, 1, tzinfo=UTC)


def test_as_of_cutoff_never_overflows_for_the_maximum_representable_date():
    # 9999-12-31 used to reach `date.fromisoformat` and overflow computing `D + 1 day`; guard.validate_as_of now
    # refuses any year past 2100, so this must never even reach as_of_cutoff — proven end to end here.
    with pytest.raises(Exception):
        guard.validate_as_of("9999-12-31")


@pytest.mark.parametrize("as_of_input", ["2026-09-24", "2026-09-24T12:00:00Z", "2026-09-24T12:00:00+05:30"],
                         ids=["date", "instant-z", "instant-offset"])
def test_as_of_cutoff_accepts_exactly_what_guard_validate_as_of_returns(as_of_input):
    normalized = guard.validate_as_of(as_of_input)
    cutoff = versions.as_of_cutoff(normalized)
    assert isinstance(cutoff, datetime) and cutoff.tzinfo is not None


def test_as_of_cutoff_rejects_a_naive_instant_string():
    with pytest.raises(ValueError):
        versions.as_of_cutoff("2026-09-24T12:00:00")


def test_is_visible_is_unchanged_by_the_instant_cutoff_support():
    # is_visible itself takes no as_of string at all — only datetimes — so it needs no change; this pins that.
    start = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
    cutoff = versions.as_of_cutoff("2026-09-24T12:00:00.000001+00:00")
    assert versions.is_visible(start, versions.CURRENT_VALID_TO, cutoff) is True


def test_current_valid_to_is_a_real_future_datetime_not_a_string():
    assert isinstance(versions.CURRENT_VALID_TO, datetime)
    assert versions.CURRENT_VALID_TO.tzinfo is not None
    assert versions.CURRENT_VALID_TO.year == 9999


def test_is_visible_true_strictly_inside_the_window():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 6, 1, tzinfo=UTC)
    cutoff = datetime(2026, 3, 1, tzinfo=UTC)
    assert versions.is_visible(start, end, cutoff) is True


def test_is_visible_false_before_valid_from():
    start = datetime(2026, 3, 1, tzinfo=UTC)
    end = versions.CURRENT_VALID_TO
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    assert versions.is_visible(start, end, cutoff) is False


def test_is_visible_false_once_valid_to_is_strictly_before_the_cutoff():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    cutoff = datetime(2026, 3, 2, tzinfo=UTC)
    assert versions.is_visible(start, end, cutoff) is False


def test_is_visible_true_when_the_cutoff_lands_exactly_on_valid_to():
    # valid_to >= cutoff (not strictly greater): the row was still in effect for the whole of its last visible day.
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    cutoff = datetime(2026, 3, 1, tzinfo=UTC)
    assert versions.is_visible(start, end, cutoff) is True


def test_is_visible_false_when_the_cutoff_lands_exactly_on_valid_from():
    # valid_from < cutoff (strictly less): a row that starts exactly at the cutoff is not yet visible.
    start = datetime(2026, 3, 1, tzinfo=UTC)
    end = versions.CURRENT_VALID_TO
    cutoff = datetime(2026, 3, 1, tzinfo=UTC)
    assert versions.is_visible(start, end, cutoff) is False


def test_is_visible_true_for_a_current_row_at_any_cutoff_after_valid_from():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    far_future_cutoff = datetime(2100, 1, 1, tzinfo=UTC)
    assert versions.is_visible(start, versions.CURRENT_VALID_TO, far_future_cutoff) is True

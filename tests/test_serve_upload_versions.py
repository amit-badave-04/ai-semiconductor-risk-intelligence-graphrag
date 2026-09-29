"""``uploads.versions``: pure version bookkeeping for upload workspaces (M4, docs/v2/M4_PLAN.md 4.2, 14.4)."""

from datetime import UTC, datetime

import pytest

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

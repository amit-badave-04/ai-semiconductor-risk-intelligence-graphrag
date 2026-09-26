"""One deterministic answer-expectation checker shared by the benchmark runner and the bake-off scorer."""

import pytest

from semigraph.eval.expect import check_expectation, parse_percentages


def test_value_accepts_any_stated_number_within_half_a_percent_in_any_scale():
    expect = {"value": 215938000000}
    assert check_expectation(expect, "Revenue was $215.9 billion.")
    assert check_expectation(expect, "It was 215,938,000,000 USD [xbrl:1045810:revenue:2026-01-25].")
    assert not check_expectation(expect, "Revenue was $190 billion.")


def test_a_number_that_ends_a_sentence_still_counts():
    assert check_expectation({"value": 130497000000}, "Nvidia's revenue for fiscal 2025 was $130.497 billion.")


def test_values_requires_every_listed_amount():
    expect = {"values": [215938000000, 120067000000]}
    assert check_expectation(expect, "Revenue $215.9 billion; net income $120.1 billion.")
    assert not check_expectation(expect, "Revenue $215.9 billion; net income was not found.")


def test_pct_reads_percent_signs_within_a_tenth_of_a_point_and_ignores_bare_numbers():
    expect = {"pct": 43.2}
    assert check_expectation(expect, "R&D grew +43.2% year over year.")
    assert check_expectation(expect, "That is a 43.3 % increase.")
    assert not check_expectation(expect, "R&D grew 43.2 billion.")          # not a percentage
    assert not check_expectation(expect, "R&D grew 45.0%.")


def test_any_of_is_a_case_insensitive_substring_match():
    assert check_expectation({"any_of": ["TSMC", "Samsung"]}, "Nvidia depends on tsmc.")
    assert not check_expectation({"any_of": ["TSMC"]}, "Nvidia depends on Intel.")


def test_several_keys_must_all_hold():
    expect = {"value": 215938000000, "pct": 65.5}
    assert check_expectation(expect, "$215.9 billion, up 65.5%.")
    assert not check_expectation(expect, "$215.9 billion, up 10%.")


def test_no_recognised_key_is_an_error_not_a_silent_pass():
    with pytest.raises(ValueError, match="expect"):
        check_expectation({"unknown": 1}, "anything")


def test_parse_percentages_handles_signs_spaces_and_commas():
    assert parse_percentages("up +43.2% and down -1.5 % but 2,000 units") == [43.2, -1.5]


# --- signs: the number parser reads magnitudes, so direction is its own check -----------------------------------

def test_a_negative_value_matches_its_magnitude_and_needs_a_loss_word_via_direction():
    expect = {"value": -267000000, "direction": "down"}
    assert check_expectation(expect, "Intel reported a net loss of $267 million.")
    assert not check_expectation(expect, "Intel's net income was $267 million, an increase.")


def test_pct_compares_magnitudes_and_direction_carries_the_sign():
    down = {"pct": -0.5, "direction": "down"}
    assert check_expectation(down, "Revenue declined 0.5% year over year.")
    assert check_expectation(down, "Revenue changed by -0.5%.")
    assert not check_expectation(down, "Revenue rose 0.5% year over year.")
    up = {"pct": 65.5, "direction": "up"}
    assert check_expectation(up, "Revenue grew 65.5%.")
    assert check_expectation(up, "Revenue was up +65.5%.")
    assert not check_expectation(up, "Revenue fell 65.5%.")


def test_direction_words_are_matched_as_whole_words():
    assert not check_expectation({"pct": 5.0, "direction": "up"}, "The supply chain shifted 5.0%.")


def test_direction_alone_is_not_an_expectation_and_a_bad_direction_is_an_error():
    with pytest.raises(ValueError):
        check_expectation({"direction": "up"}, "up")
    with pytest.raises(ValueError, match="direction"):
        check_expectation({"pct": 1.0, "direction": "sideways"}, "1.0%")


def test_any_of_treats_a_hyphen_like_a_space():
    """R1 (deployed run): "advanced-computing export requirements" is the same concept as "Advanced Computing"."""
    assert check_expectation({"any_of": ["Advanced Computing"]}, "the October 2023 advanced-computing export requirements")
    assert check_expectation({"any_of": ["Foreign-Produced Direct Product"]}, "the foreign produced direct product rule")
    assert not check_expectation({"any_of": ["Advanced Computing"]}, "the advancedcomputing rule")

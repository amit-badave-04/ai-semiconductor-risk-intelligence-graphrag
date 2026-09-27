"""``tests/cliout.plain``: CLI error assertions must not depend on how Typer renders (plain locally, a wrapped ANSI box on GitHub Actions)."""

from cliout import plain

# The output of ``semigraph build-graph --as-of 2026-06-30`` on GitHub Actions (typer forces rich rendering there), verbatim from the failed run.
CI_OUTPUT = (
    "\x1b[33mUsage: \x1b[0mroot build-graph [OPTIONS]\n\x1b[2mTry \x1b[0m\x1b[2;34m'root build-graph \x1b[0m\x1b[1;2;34m-\x1b[0m"
    "\x1b[1;2;34m-help\x1b[0m\x1b[2;34m'\x1b[0m\x1b[2m for help.\x1b[0m\n"
    "\x1b[31m┌─\x1b[0m\x1b[31m Error \x1b[0m\x1b[31m" + "─" * 68 + "\x1b[0m\x1b[31m─┐\x1b[0m\n"
    "\x1b[31m│\x1b[0m Invalid value: the data lake holds data (2026-09-24) newer than \x1b[1;36m-\x1b[0m\x1b[1;36m-as\x1b[0m\x1b[1;36m-of\x1b[0m"
    "     \x1b[31m│\x1b[0m\n"
    "\x1b[31m│\x1b[0m 2026-06-30; re-ingest with \x1b[1;36m-\x1b[0m\x1b[1;36m-as\x1b[0m\x1b[1;36m-of\x1b[0m or declare a later date"
    "                  \x1b[31m│\x1b[0m\n"
    "\x1b[31m└" + "─" * 77 + "┘\x1b[0m\n")


def test_the_wrapped_coloured_error_box_of_a_ci_run_reads_as_one_plain_sentence():
    text = plain(CI_OUTPUT)

    assert "newer than --as-of 2026-06-30; re-ingest with --as-of or declare a later date" in text
    assert "\x1b" not in text and "│" not in text and "─" not in text


def test_a_plain_local_run_is_unchanged_apart_from_whitespace():
    local = "Error: the data lake holds data (2026-09-24) newer than --as-of 2026-06-30\n"

    assert plain(local) == "Error: the data lake holds data (2026-09-24) newer than --as-of 2026-06-30"


def test_words_the_box_split_across_two_lines_are_not_glued_together():
    assert plain("│ newer than │\n│ --as-of │") == "newer than --as-of"

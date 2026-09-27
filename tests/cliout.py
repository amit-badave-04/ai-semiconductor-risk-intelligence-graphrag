"""The plain text of a Typer CLI run's output, whatever the terminal rendering.

On GitHub Actions Typer forces rich rendering (``typer.rich_utils.FORCE_TERMINAL`` is set when ``GITHUB_ACTIONS`` is): ANSI colour codes,
and a parameter error drawn as an 80-column box that wraps the message and colours each option name (``--as-of`` arrives as ``-``,
``-as`` and ``-of``). ``"newer than --as-of" in result.output`` therefore holds on a developer machine and fails in CI. Tests that assert
on a usage / parameter error compare :func:`plain` of the output instead.
"""

import re

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BOX = re.compile("[│┌┐└┘╭╮╰╯─]")   # the light and rounded box-drawing characters rich uses


def plain(output: str) -> str:
    """``output`` without colour codes or box characters, its whitespace (and the panel's line wraps) collapsed to single spaces."""
    return " ".join(_BOX.sub(" ", _ANSI.sub("", output)).split())

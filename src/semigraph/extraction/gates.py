"""Programmatic anti-fabrication gate — ported from notebooks 07/12.

Runs BEFORE the critic: an evidence quote that does not appear verbatim in the
chunk (after whitespace/smart-quote normalization) is dropped without spending
a single critic token. Pure functions, no I/O.
"""

import re

_NORM_RE = re.compile(r"[\s’'\"“”]+")


def normalize(s: str) -> str:
    """Collapse whitespace runs and straight/smart quotes to single spaces, casefold."""
    return _NORM_RE.sub(" ", s).strip().lower()


def quote_in_chunk(quote: str, chunk_text: str) -> bool:
    """Programmatic anti-fabrication gate: the evidence quote must actually appear in the chunk."""
    return normalize(quote) in normalize(chunk_text)

"""Deterministic answer expectations for benchmark questions (one checker for the runner and the bake-off scorer).

A question's ``expect`` object holds one or more of these keys; ALL that are present must hold:

- ``value``: one amount; the answer must state some number within 0.5% of it (any scale: ``$215.9 billion``).
- ``values``: a list of amounts; every one must be stated.
- ``pct``: a percentage (signed, e.g. the code-computed year-over-year); the answer must state a number followed by
  ``%`` within 0.1 percentage points. A bare number never satisfies it.
- ``any_of``: case-insensitive substrings; at least one must occur.
- ``direction`` (``up`` / ``down``): the answer must use a word of that direction. The number parsers read magnitudes
  (``-267`` and ``267`` are the same to them), so the sign of a ``value`` / ``pct`` is checked here, not there. It only
  qualifies another key.
"""

import re
from collections.abc import Mapping

VALUE_TOLERANCE = 0.005
PCT_TOLERANCE_POINTS = 0.1
_PCT_RE = re.compile(r"([-+]?\d[\d,]*(?:\.\d+)?)\s*%")
_KEYS = ("value", "values", "pct", "any_of")
_DIRECTION_RE = {
    "up": re.compile(r"\b(increas\w*|grew|grow\w*|rose|ris(?:e|es|en|ing)|up|higher|gain\w*|expand\w*)\b|\+\s*\$?\d", re.I),
    "down": re.compile(r"\b(decreas\w*|declin\w*|fell|fall\w*|drop\w*|down|lower|reduc\w*|contract\w*|loss(?:es)?|negative)\b"
                       r"|[-\u2212]\s*\$?\d", re.I),
}

NUM_PAT = re.compile(r"\$?([0-9][0-9,\.]*)\s*(billion|bn|b\b|million|mn|m\b|trillion)?", re.I)


def parse_numbers(text: str) -> list[float]:
    """Extract dollar/scale-suffixed numbers as absolute values
    (notebook 14 numeric-consistency check)."""
    out = []
    for m in NUM_PAT.finditer(text.replace(",", "")):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        mult = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6,
                "m": 1e6, "trillion": 1e12}.get(unit, 1)
        out.append(v * mult)
    return out


def parse_percentages(text: str) -> list[float]:
    """Every ``<number>%`` in the text as a float (sign kept, thousands commas removed)."""
    return [float(m.group(1).replace(",", "")) for m in _PCT_RE.finditer(text)]


def _states_amount(answer: str, target: float) -> bool:
    """Magnitude match (``parse_numbers`` returns absolute values; the sign is the ``direction`` check)."""
    return any(abs(v - abs(target)) / abs(target) < VALUE_TOLERANCE for v in parse_numbers(answer))


def check_expectation(expect: Mapping, answer: str) -> bool:
    """True when the answer satisfies every expectation key in ``expect``; ValueError when it has none."""
    if not any(k in expect for k in _KEYS):
        raise ValueError(f"expect has none of {_KEYS}: {dict(expect)!r}")
    if "direction" in expect and expect["direction"] not in _DIRECTION_RE:
        raise ValueError(f"expect.direction must be 'up' or 'down', got {expect['direction']!r}")
    checks = []
    if "value" in expect:
        checks.append(_states_amount(answer, expect["value"]))
    if "values" in expect:
        checks.append(all(_states_amount(answer, v) for v in expect["values"]))
    if "pct" in expect:
        target = float(expect["pct"])
        checks.append(any(abs(abs(p) - abs(target)) <= PCT_TOLERANCE_POINTS + 1e-9 for p in parse_percentages(answer)))
    if "direction" in expect:
        checks.append(bool(_DIRECTION_RE[expect["direction"]].search(answer)))
    if "any_of" in expect:
        lowered = answer.lower()
        checks.append(any(s.lower() in lowered for s in expect["any_of"]))
    return all(checks)

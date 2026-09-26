"""Deterministic checks on a drafted answer: what must hold before a cheap model's text may be shown.

Used two ways: the service releases a cheap draft only when this returns no reasons (otherwise it escalates
to the stronger model), and the bake-off measures how often each candidate would trigger that escalation.
Pure and free — no model call, no database — and importable in the slim serving image.
"""

import re

# Wordings that mark a deliberate "the corpus cannot answer this" reply (verbatim notebook 14 pattern).
REFUSAL_RE = re.compile(r"does not (contain|include|provide)|not available|no (information|data|filings)|"
                        r"cannot (be )?(determin|answer|find)|isn't|is not in the (context|filings|corpus)|not an SEC filer", re.I)


def verify_answer(text: str, cited: set[str], valid_ids: set[str], finish_reason: str | None) -> list[str]:
    """Reasons the draft must not be released as is (empty list = it may be).

    - ``empty``: nothing was produced.
    - ``truncated``: the model ran out of output budget mid-answer.
    - ``invalid_citation``: a cited id is not in the retrieved context (a fabricated source).
    - ``no_citation``: no source is cited and the answer is not an explicit refusal.
    """
    reasons = []
    if not text.strip():
        return ["empty"]
    if finish_reason == "length":
        reasons.append("truncated")
    if cited - valid_ids:
        reasons.append("invalid_citation")
    if not cited and not REFUSAL_RE.search(text):
        reasons.append("no_citation")
    return reasons

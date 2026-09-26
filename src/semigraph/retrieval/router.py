"""Deterministic routing: which questions must go straight to the strong model.

The model bake-off found every cheaper model matching Sonnet on numeric, dependency, regulatory and risk
questions but failing the "how has the risk profile evolved" question — it relays the dropped-lineage block
as fact instead of reasoning over it. Questions about how disclosures CHANGED are this product's
differentiator, so they skip the cheap draft entirely. Over-routing only costs money, never quality, so the
pattern errs towards matching. Pure and free.
"""

import re

_CHANGE_OVER_TIME = re.compile(
    r"\bevolv\w*|\bover time\b|\btrends?\b|\bhistor(?:y|ical\w*)\b|"
    r"\bstop(?:ped)?\s+(?:disclos|report)\w*|\bno longer\s+(?:disclos|report)\w*|"
    r"\bdropp(?:ed|ing)\b|\bremoved\b|"
    r"\bnew(?:ly)?\b[^?.]{0,60}\b(?:appear\w*|risks?|disclos\w*)|\bappeared\b|"
    r"\b(?:compared|versus|vs\.?)\s+(?:to|with)?\s*(?:the\s+)?(?:prior|previous|earlier|last)\b|"
    r"\byear[- ]over[- ]year\b|\bacross\s+(?:its|their|the|recent|multiple)\b[^?.]{0,40}\b(?:reports?|filings?|10-?Ks?|years)\b",
    re.I)


def needs_strong_model(question: str) -> bool:
    """True when the question is about change in disclosures over time (route to the strong model directly)."""
    return bool(_CHANGE_OVER_TIME.search(question))

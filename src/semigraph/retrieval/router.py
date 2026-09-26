"""Deterministic routing: which questions must go straight to the strong model.

The model bake-off found every cheaper model matching Sonnet on numeric, dependency, regulatory and risk
questions but failing the "how has the risk profile evolved" question — it relays the dropped-lineage block
as fact instead of reasoning over it. Questions about how disclosures CHANGED are this product's
differentiator, so they skip the cheap draft entirely. Over-routing only costs money, never quality, so the
rules err towards matching; the verifier cannot catch a well-cited but shallow answer, which is why this
cannot be left to it.

Three rules, any of which routes the question:

1. an unmistakable change-over-time phrase ("evolved", "over time", "stopped disclosing", "appeared", ...);
2. a change verb together with a disclosure noun ("How have the risk disclosures changed?", "What risks did AMD
   add in its latest 10-K?") — the noun keeps plain numeric growth questions ("revenue grew") on the cheap path;
3. a time-anchored comparison together with a disclosure noun ("Compare TSMC's 2023 and 2025 risk factors",
   "risk grown since 2022") — a comparison across companies with no year stays cheap.
"""

import re

_CHANGE_PHRASE = re.compile(
    r"\bevolv\w*|\bover time\b|\btrends?\b|\bhistor(?:y|ical\w*)\b|"
    r"\bstop(?:ped)?\s+(?:disclos|report|mention)\w*|\bno longer\s+(?:disclos|report|mention)\w*|"
    r"\bdropp(?:ed|ing)\b|\bremoved\b|"
    r"\bnew(?:ly)?\b[^?.]{0,60}\b(?:appear\w*|risks?|disclos\w*)|\bappeared\b|"
    r"\b(?:compared|versus|vs\.?)\s+(?:to|with)?\s*(?:the\s+)?(?:prior|previous|earlier|last)\b|"
    r"\byear[- ]over[- ]year\b|\bacross\s+(?:its|their|the|recent|multiple)\b[^?.]{0,40}\b(?:reports?|filings?|10-?Ks?|years)\b",
    re.I)

_CHANGE_VERB = re.compile(
    r"\b(?:chang\w*|add(?:ed|s|ing)?|differ\w*|grow\w*|grew|shift\w*|introduc\w*|emerg\w*|disappear\w*|"
    r"remov\w*|dropp?\w*|new(?:ly)?)\b", re.I)

_DISCLOSURE_NOUN = re.compile(
    r"\b(?:risks?|risk factors?|disclos\w*|10-?K|10-?Q|annual reports?|filings?|lineages?)\b", re.I)

_YEAR = r"(?:19|20)\d\d"
_TIME_ANCHOR = re.compile(
    rf"\b(?:compar\w*|versus|vs\.?|since|between|relative to|earlier|prior|previous)\b[^?.]{{0,80}}?\b{_YEAR}\b|"
    rf"\b{_YEAR}\b[^?.]{{0,80}}?\b(?:and|to|vs\.?|versus|through)\b[^?.]{{0,20}}?\b{_YEAR}\b", re.I)


def needs_strong_model(question: str) -> bool:
    """True when the question is about change in disclosures over time (route to the strong model directly)."""
    if _CHANGE_PHRASE.search(question):
        return True
    if not _DISCLOSURE_NOUN.search(question):
        return False
    return bool(_CHANGE_VERB.search(question) or _TIME_ANCHOR.search(question))

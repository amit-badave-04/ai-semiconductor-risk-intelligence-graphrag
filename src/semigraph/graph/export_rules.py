"""Export-control rule relevance and AFFECTED_BY selection — pure, no Neo4j.

A KEYWORD HEURISTIC, documented as such (notebook 12 stage 7): a company is
linked to a relevant rule when it discloses a current 'Export Controls' risk
whose evidence text contains keywords for the rule's kind. It is keyword
co-occurrence, not verified causal impact.
"""

import re
from collections.abc import Iterable, Mapping

LEGACY_KIND = "legacy"  # a cached rule that predates per-rule classification
MAX_AFFECTED_BY_PER_COMPANY = 15  # most recent relevant rules linked per company
MAX_EDGE_EVIDENCE_CHUNKS = 10  # audit chunk ids carried on one AFFECTED_BY edge
# A risk counts as an export-control risk when its (free-text, extractor-chosen) category CONTAINS this,
# case-insensitively: "Export Controls", "regulatory/export control", "Legal/Regulatory - Export Controls" ...
EXPORT_CONTROLS_CATEGORY = "export control"

# rule kind -> evidence-text keywords (documented heuristic: keyword co-occurrence
# between a company's current export-control risk text and the rule's topic — not
# verified causal impact). Kinds absent here (entity_list_additions, other) never link.
KIND_EVIDENCE_KEYWORDS: dict[str, list[str]] = {
    "advanced_computing": ["advanced computing", "ai chip", "accelerator", "gpu", "h100", "h200", "h20", "a100"],
    "semiconductor_equipment": ["manufacturing equipment", "semiconductor manufacturing", "lithography"],
    "affiliates_rule": ["entity list", "affiliate"],
    "licensing_policy": ["license", "licensing", "export control"],
    "ai_model_controls": ["artificial intelligence", "ai diffusion", "model weights"],
}


def _keyword_regex(keyword: str) -> str:
    """Word-start match; alphanumeric product codes (h100, a100 ...) must be whole tokens."""
    body = re.escape(keyword)
    return rf"\b{body}\b" if any(c.isdigit() for c in keyword) else rf"\b{body}"


_KIND_PATTERNS = {
    kind: re.compile("|".join(_keyword_regex(k) for k in kws), re.IGNORECASE)
    for kind, kws in KIND_EVIDENCE_KEYWORDS.items()
}

# LEGACY: rule-title keyword -> evidence-text keywords (notebook 12 stage 7 heuristic),
# still used for cached rules that carry no `kind`.
TOPIC_KEYWORDS = {
    "entity list": ["entity list"],
    "advanced computing": ["advanced computing", "ai chip", "accelerator"],
    "semiconductor manufacturing": ["manufacturing equipment", "semiconductor manufacturing"],
    "artificial intelligence": ["artificial intelligence", "ai diffusion"],
}


def normalize_rule(rule: Mapping) -> dict:
    """A rule with ``kind`` / ``topics`` / ``relevant`` filled in.

    Rules classified at acquisition keep their fields. A legacy cached rule
    (no ``kind``) falls back to the pre-classification behaviour: kind
    ``legacy``, relevant, no topics. A classified rule missing only ``relevant``
    is relevant when its kind can be matched against evidence.
    """
    out = dict(rule)
    if "kind" not in out:
        return {**out, "kind": LEGACY_KIND, "topics": list(out.get("topics") or []),
                "relevant": bool(out.get("relevant", True))}
    return {**out, "topics": list(out.get("topics") or []),
            "relevant": bool(out["relevant"]) if "relevant" in out else out["kind"] in KIND_EVIDENCE_KEYWORDS}


def rule_matches_evidence(rule: Mapping, text: str) -> bool:
    """Does this evidence text contain keywords for the rule's kind?

    ``legacy`` rules use the notebook-12 title-topic matching; kinds without
    keywords (entity_list_additions, other) never match.
    """
    if rule["kind"] == LEGACY_KIND:
        title_l, text_l = rule["title"].lower(), text.lower()
        return any(topic in title_l and any(k in text_l for k in kws)
                   for topic, kws in TOPIC_KEYWORDS.items())
    pattern = _KIND_PATTERNS.get(rule["kind"])
    return bool(pattern and pattern.search(text))


def select_affected_rules(chunks: list[dict], rules: Iterable[Mapping],
                          cap: int = MAX_AFFECTED_BY_PER_COMPANY) -> list[dict]:
    """AFFECTED_BY link rows (without the company) for ONE company's current
    export-control evidence chunks (``[{chunk_id, text}]``).

    Only relevant rules whose kind keywords appear in some chunk are linked;
    the edge carries the matching chunk ids (capped). At most ``cap`` links are
    kept, newest rule first (ties: higher document number), so the count cannot
    grow with the number of rules on the Federal Register.
    """
    links = []
    for rule in rules:
        if not rule["relevant"]:
            continue
        matched = [c["chunk_id"] for c in chunks if rule_matches_evidence(rule, c["text"])]
        if matched:
            links.append({"rule_id": rule["document_number"], "date": rule["publication_date"],
                          "chunks": matched[:MAX_EDGE_EVIDENCE_CHUNKS]})
    links.sort(key=lambda link: (link["date"], link["rule_id"]), reverse=True)
    return links[:cap]


def build_affected_by_rows(exposures: Mapping[int, list[dict]], rules: Iterable[Mapping]) -> list[dict]:
    rules = list(rules)
    return [{"cik": cik, **link}
            for cik, chunks in sorted(exposures.items())
            for link in select_affected_rules(chunks, rules)]

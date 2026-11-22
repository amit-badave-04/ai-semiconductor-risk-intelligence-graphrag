"""R8: claim support, a deterministic EVAL-ONLY check (never imported by ``semigraph.serve``).

For each sentence of an answer that cites ids, the figures it states and the companies it names must occur in the text
behind the ids it cites (``sources_from_context``: id -> the context lines / excerpt under that id):

- a cited id with no text behind it (hallucinated, or an id with an empty excerpt) makes the sentence UNSUPPORTED;
- every dollar / scaled amount (``$215.9 billion``, ``60 million``) must match an amount in the cited text (0.5%) and every
  percentage a percentage in it (0.1 points), reusing ``eval.expect``'s parsers;
- every canonical company the sentence names (name or alias from the packaged canonical entities) must be named in the
  cited text under some name or alias of that company;
- a sentence with no citation, or a negative claim ("not provided", "no ..."), has no checkable claim and is skipped.

Rate = supported / (supported + unsupported); None when the answer has no checkable sentence. Known limits: a figure
without ``$``, a scale word or ``%`` is not checked, and the company match is by name, not by meaning.
"""

import re
from functools import cache

from ..artifacts import load_canonical_entities
from ..retrieval.ids import CITE_RE
from .expect import _ANY_CITE_RE, _NEGATION_RE, _sentences, parse_numbers, parse_percentages

AMOUNT_TOLERANCE = 0.005
PCT_TOLERANCE_POINTS = 0.1
_AMOUNT_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:billion|million|trillion|bn|mn))?"
                        r"|\b\d[\d,]*(?:\.\d+)?\s*(?:billion|million|trillion)\b", re.I)


@cache
def _entity_patterns() -> list[tuple[re.Pattern, str]]:
    """Per canonical company: a regex over its name and aliases (whole words, case-insensitive) and its zero-padded CIK,
    which the id of a metric or a filing passage embeds (``xbrl:1045810:...``, ``0001045810-26-...``)."""
    out = []
    for name, spec in load_canonical_entities().items():
        names = sorted({name, *spec.get("aliases", [])}, key=len, reverse=True)
        pattern = r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b"
        out.append((re.compile(pattern, re.I), f"{spec['cik']:010d}" if spec.get("cik") else ""))
    return out


_ANY_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?(?:\s*(?:billion|million|trillion|bn|mn))?", re.I)


def _amounts(text: str, *, any_number: bool = False) -> list[float]:
    """Amounts stated in ``text``: only ``$`` / scaled ones for an answer, every number for a source (a metric line prints
    ``215,938,000,000 USD`` bare)."""
    rx = _ANY_NUMBER_RE if any_number else _AMOUNT_RE
    return [v for m in rx.finditer(text.replace(",", "")) for v in parse_numbers(m.group(0))]


def _figure_supported(figure: float, pool: list[float]) -> bool:
    return any(abs(v - figure) / figure < AMOUNT_TOLERANCE for v in pool if figure)


_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_EMPHASIS_RE = re.compile(r"\*+|_{2,}|`+|(?<!\w)_|_(?!\w)")


def _plain(text: str) -> str:
    """``text`` without markdown emphasis (``**`` ``__`` ``*`` ``_`` backticks) and a leading list marker, so a bulleted
    ``- **TSMC**`` is matched as the name ``TSMC`` (``_`` between word characters, as in an id, stays)."""
    return _EMPHASIS_RE.sub("", _LIST_MARKER_RE.sub("", text))


def _sentence_supported(sentence: str, sources: dict[str, str]) -> bool | None:
    """True / False, or None when the sentence has no checkable claim (no citation, or a negative claim)."""
    ids = CITE_RE.findall(sentence)
    body = _plain(_ANY_CITE_RE.sub("", sentence))
    if not ids or _NEGATION_RE.search(body):
        return None
    texts = [sources.get(i, "") for i in ids]
    if not all(t.strip() for t in texts):
        return False
    cited = "\n".join(texts)
    pool = _amounts(cited, any_number=True)
    if not all(_figure_supported(v, pool) for v in _amounts(body)):
        return False
    pcts = parse_percentages(cited)
    if not all(any(abs(abs(p) - abs(q)) <= PCT_TOLERANCE_POINTS + 1e-9 for q in pcts) for p in parse_percentages(body)):
        return False
    return all(rx.search(cited) or any(_id_embeds_cik(i, cik) for i in ids)
               for rx, cik in _entity_patterns() if rx.search(body))


def _id_embeds_cik(citation: str, cik10: str) -> bool:
    return bool(cik10) and (citation.startswith(cik10) or citation.split(":")[:2] == ["xbrl", cik10.lstrip("0")])


def claim_support(answer: str, sources: dict[str, str]) -> dict:
    """``{"supported", "unsupported" (count), "unsupported_sentences", "rate"}`` for one answer."""
    verdicts = [(s, _sentence_supported(s, sources)) for s in _sentences(answer)]
    bad = [s for s, v in verdicts if v is False]
    good = sum(1 for _, v in verdicts if v is True)
    total = good + len(bad)
    return {"supported": good, "unsupported": len(bad), "unsupported_sentences": bad, "rate": good / total if total else None}


def _textful_sources(context: str, sources: dict[str, str]) -> dict[str, str]:
    """R8 (the D1 shape): an id seen only on RELATIONSHIPS edge lines (no quote line, no excerpt) has NO text, so a
    sentence citing it is unsupported. Same edge/text classification as verify's ``cited_without_text``."""
    from ..retrieval.verify import _edge_and_text_ids

    edge, _, _, texted = _edge_and_text_ids(context)
    edge_only = edge - texted
    return {i: ("" if i in edge_only else t) for i, t in sources.items()}


def claim_support_rate(rows: list[dict]) -> dict:
    """The R8 rate over deployed rows (each needs ``context``; a row without one RAISES, it never silently passes).
    Errored rows with no answer are skipped. ``rate`` is None when nothing was checkable."""
    from ..retrieval.answerer import sources_from_context

    supported = unsupported = 0
    per_row: dict[str, list[str]] = {}
    for r in rows:
        if r.get("error") and not r.get("answer"):
            continue
        if not isinstance(r.get("context"), str):
            raise ValueError(f"row {r.get('id')!r} has no context: claim support cannot be scored (R8a)")
        res = claim_support(r.get("answer") or "", _textful_sources(r["context"], sources_from_context(r["context"])))
        supported += res["supported"]
        unsupported += res["unsupported"]
        if res["unsupported_sentences"]:
            per_row[r["id"]] = res["unsupported_sentences"]
    total = supported + unsupported
    return {"supported": supported, "unsupported": unsupported, "rate": supported / total if total else None,
            "unsupported_by_row": per_row}

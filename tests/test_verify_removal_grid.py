"""Generated grids over the removal-claim check: every combination of a few openings, connectives and claims.

The hand-written tables (test_verify_removal_negation.py, test_verify_removal_regressions.py) pin the sentences two adversaries
found. These grids pin the SHAPES: one of them (a negated opening, a connective, then a positive claim) found 60 claims the
check let through that no adversary had written. Each grid is one test that lists the rows it gets wrong.

- a negated or none-quantified opening, a connective that starts a new statement, and a positive removal claim citing an id no
  removed list supports: every row must be flagged;
- honest negated and none-quantified sentences (with an id or without), in a comparison and in an answer about anything else:
  no row may be flagged;
- a removed-list heading with a body that names an unsupported id must be flagged, one with none found or a supported id must not;
- a claim that names an item under a quoted list label must be flagged, a mention of the label must not.
"""

import itertools
from functools import lru_cache

from semigraph.retrieval.answerer import CITE_RE, sources_from_context
from semigraph.retrieval.verify import answer_checks
from test_verify_removal_negation import IDS
from test_verify_removal_regressions import CONTEXTS

K, R, RISK = IDS["K"], IDS["R"], IDS["RISK"]
SHOWN = 12


@lru_cache(maxsize=None)
def _context(name):
    return CONTEXTS[name]()


def flagged(context, text):
    _, ctx, valid = _context(context)
    sources = sources_from_context(ctx) if context != "plain" else {}
    return bool(answer_checks(text, set(CITE_RE.findall(text)), valid, ctx, sources=sources).removal_claims)


def _rows_wrong(rows, want_flagged):
    return [f"[{context}] {text!r}" for context, text in rows if flagged(context, text) != want_flagged]


CONNECTIVES = [" and ", ", but ", "; ", ", so ", " but ", ", and ", " — ", ": ", ". ", ", while ", " although ", ", however, ", ", yet ",
               " -- ", " whereas ", " because ", ", which is why ", " and then ", " and also ", " and, in the 10-K, ", " though "]
POSITIVE_CLAIMS = ["it removed the indebtedness risk factor", "the Russia risk factor was dropped", "Nvidia deleted the risk factor",
                   "the export risk factor no longer appears in the newer 10-K", "the sentence's wording was not found in the newer filing",
                   "the indebtedness risk factor was removed", "Nvidia eliminated the Taiwan risk factor", "one risk factor was removed"]

NEGATED_OPENINGS = [f"{who} {neg} {what}" for who, neg in (("Nvidia", "did not"), ("Nvidia", "never"), ("The comparison", "cannot"),
                                                          ("The text check", "did not"))
                    for what in ("add risks", "reword the export risk factor", "say why", "show that anything was reworded",
                                 "identify any new risk factor")]
QUANTIFIED_OPENINGS = [f"{who} {noun}" for who in ("Nvidia added no", "There were no", "The check found no", "None of the")
                       for noun in ("new risk factors", "changes", "wording changes")]


def test_a_positive_claim_after_a_negated_or_quantified_opening_and_a_connective_is_flagged():
    rows = [("removal", f"{opening}{connective}{claim} [{K}].")
            for opening, connective, claim in itertools.product(NEGATED_OPENINGS + QUANTIFIED_OPENINGS, CONNECTIVES, POSITIVE_CLAIMS)]
    wrong = _rows_wrong(rows, want_flagged=True)
    assert not wrong, f"{len(wrong)} of {len(rows)} passed: {wrong[:SHOWN]}"


SUBJECTS = ["Nvidia", "The company", "The filing", "The USG", "It", "Management", "The comparison"]
NEGATORS = ["did not", "does not", "has not", "never", "cannot", "could not", "has never", "had not", "would not", "did not yet",
            "did not officially", "did not, however,", "did not, in 2025,", "did not, according to the filing,", "did not publicly",
            "did not really", "will not", "was not going to", "is not going to", "failed to", "did not even", "did not simply"]
REMOVALS = ["remove the tariff", "drop the license requirement", "eliminate the H20 restriction", "delete the risk factor",
            "discontinue the product", "omit the sentence", "remove the risk factor", "drop the risk", "eliminate the disclosure"]
NONE_SUBJECTS = ["No tariff was", "No tariffs were", "None of the tariffs were", "Zero risk factors were", "No risk factor has been",
                 "Nothing was", "No product was"]
PASSIVES = ["removed", "dropped", "eliminated", "deleted", "discontinued", "omitted"]


def test_an_honest_negated_or_none_quantified_sentence_is_not_flagged_in_any_context():
    honest = [f"{who} {neg} {removal} [{{id}}]." for who, neg, removal in itertools.product(SUBJECTS, NEGATORS, REMOVALS)]
    honest += [f"{who} {passive} [{{id}}]." for who, passive in itertools.product(NONE_SUBJECTS, PASSIVES)]
    rows = [(context, text.replace("{id}", RISK if context == "plain" else K)) for context in ("plain", "removal") for text in honest]
    wrong = _rows_wrong(rows, want_flagged=False)
    assert not wrong, f"{len(wrong)} of {len(rows)} flagged: {wrong[:SHOWN]}"


HEADINGS = ["### No longer appears as a separate risk factor", "## No longer appears as a separate risk factor:",
            "**No longer appears as a separate risk factor:**", "**No longer appears as a separate risk factor**",
            "No longer appears as a separate risk factor:", "- **No longer appears as a separate risk factor:**",
            "### Removed risk factors", "**Removed risk factors:**", "Removed risk factors:", "### Risk factors that no longer appear",
            "### Passages whose wording was not found in the newer filing",
            "**Passages of surviving risk factors whose wording was not found in the newer filing:**",
            "### Dropped risk factors (1)", "**Dropped:**", "### Removed / dropped risk factors"]
NAMING_BODIES = ["\n- Taiwan [K]", "\n- None found.\n- Taiwan [K]", "\nTaiwan risk factor [K].", "\nNone found. Taiwan risk factor [K].",
                 "\nNone found.\nTaiwan risk factor [K].", "\n- Taiwan [K]\n- China [K]", "\n1. Taiwan [K]", "\n2. Taiwan [K]",
                 "\n| Risk | Src |\n|---|---|\n| Taiwan | [K] |", "\nThe Taiwan risk factor was removed [K].", "\n\n- Taiwan [K]",
                 "\n  - Taiwan [K]", "\n* Taiwan [K]", "\n+ Taiwan [K]", "\nTaiwan [K]", "\nTaiwan [K]\nChina [K]",
                 "\n- Taiwan [R]\n- China [K]", "\n- China [K]\n- Taiwan [R]", "\nTaiwan [R].\nChina [K]."]
HONEST_BODIES = ["\n- Taiwan [R]", "\n- None found.", "\nNone found.", "\nThe list shows none found.", "\nTaiwan risk factor [R].",
                 "\n- Taiwan [R]\n- China [R]", "\n\n- Taiwan [R]", "\n1. Taiwan [R]", "\nNone.", "\nThe text check found none.",
                 "\nNone found for this comparison.", "\n- None"]


def _fill(text):
    return text.replace("[K]", f"[{K}]").replace("[R]", f"[{R}]")


def test_a_removed_list_heading_whose_body_names_an_unsupported_id_is_flagged():
    rows = [("removal", _fill(head + body)) for head, body in itertools.product(HEADINGS, NAMING_BODIES)]
    wrong = _rows_wrong(rows, want_flagged=True)
    assert not wrong, f"{len(wrong)} of {len(rows)} passed: {wrong[:SHOWN]}"


def test_a_removed_list_heading_with_none_found_or_a_supported_id_is_not_flagged():
    rows = [("removal", _fill(head + body)) for head, body in itertools.product(HEADINGS, HONEST_BODIES)]
    wrong = _rows_wrong(rows, want_flagged=False)
    assert not wrong, f"{len(wrong)} of {len(rows)} flagged: {wrong[:SHOWN]}"


LABELS = ["No longer appears as a separate risk factor", "No longer appears as a separate paragraph",
          "Passages of surviving risk factors whose wording was not found in the newer filing",
          "Passages whose wording was not found in the newer filing"]
QUOTES = [('"', '"'), ("“", "”"), ("'", "'")]
CLAIM_FRAMES = ["The Taiwan risk factor is listed under {q}{L}{r} [K].", "Under {q}{L}{r} we see the Taiwan risk [K].",
                "The {q}{L}{r} list includes the Taiwan risk [K].", "The Taiwan risk factor appears in the {q}{L}{r} list [K].",
                "Taiwan is one of the risks in the {q}{L}{r} list [K].", "The Taiwan risk sits under {q}{L}{r} [K].",
                "The {q}{L}{r} list names the Taiwan risk factor [K].", "The Taiwan risk factor falls under {q}{L}{r} [K].",
                "Nvidia's {q}{L}{r} list has the Taiwan risk factor on it [K].", "The Taiwan risk factor is on the {q}{L}{r} list [K].",
                "The {q}{L}{r} list shows the Taiwan risk factor [K], which is not empty.",
                "The Taiwan risk factor [K], which is on the {q}{L}{r} list."]
MENTION_FRAMES = ["The {q}{L}{r} list shows none found.", "The {q}{L}{r} list is empty.", "See the {q}{L}{r} list.",
                  "Under {q}{L}{r}, the comparison shows none.", "The {q}{L}{r} list for this comparison shows none found.",
                  "The {q}{L}{r} list is empty for the fiscal 2025 and fiscal 2026 filings.",
                  "The Taiwan risk factor is not on the {q}{L}{r} list [K].", "The Taiwan risk factor is absent from the {q}{L}{r} list [K].",
                  "The {q}{L}{r} list does not include the Taiwan risk factor [K].", "Nothing appears under {q}{L}{r}.",
                  "The comparison has nothing under {q}{L}{r}.", "No item is listed under {q}{L}{r}."]


def _label_rows(frames):
    return [("removal", _fill(frame.format(q=q, L=label_text, r=r)))
            for label, (q, r), frame in itertools.product(LABELS, QUOTES, frames) for label_text in (label, label.lower())]


def test_a_claim_that_names_an_item_under_a_quoted_list_label_is_flagged():
    rows = _label_rows(CLAIM_FRAMES)
    wrong = _rows_wrong(rows, want_flagged=True)
    assert not wrong, f"{len(wrong)} of {len(rows)} passed: {wrong[:SHOWN]}"


def test_a_mention_of_a_quoted_list_label_is_not_flagged():
    rows = _label_rows(MENTION_FRAMES)
    wrong = _rows_wrong(rows, want_flagged=False)
    assert not wrong, f"{len(wrong)} of {len(rows)} flagged: {wrong[:SHOWN]}"

"""Second adversarial pass over the removal-claim check (tests/test_verify_removal_negation.py holds the first).

Two independent adversaries attacked the negation / none-quantifier / label-echo rules of ``verify.py`` and reproduced 141 honest
sentences the check flagged and 169 positive claims it let through; 108 of those claims and 27 of those sentences were the
check's own regressions against the version before the negation rules (a wider reach for negators and quantifiers, an "and"
rule, an exception carve-out). Every row of those lists is a case here, grouped by the mechanism that produced it (the
``family``). The ``independent-probe-batch-*`` families are two further batches of probes written against the fixes themselves,
with none of the adversaries' wording.

- ``PASS_CASES``  honest text the check must not flag (what the answer prompt itself asks the model to write, and its close
  relatives). Most of it is a NEGATED or "none found" statement, a copied list label, or a refusal.
- ``FLAG_CASES``  positive removal claims that no removed list supports: each cites an id the removed lists do not support (a
  reworded item, a Not matched item), or nothing at all, and must still be flagged.
- ``LIMIT_FP`` / ``LIMIT_FN``  the rows this change deliberately leaves as they are, each family with the reason: a fix would
  reopen a pinned positive claim (the safe direction is to flag), or the shape needs a parser. Pinned so that changing them is a
  decision, not an accident.

The tables are data only, so a scratch harness can import them to compare verifier versions. This file is over the 800-line soft
ceiling because of them: test data. The contexts are the ones of ``test_verify_removal_negation`` (``removal``, ``none``,
``unsettled``, ``plain``) plus ``new_reworded`` (one new and one reworded item, nothing removed), ``unsettled_only`` (one Not
matched item, nothing removed) and ``real`` (removed passages that carry the ids the deployed-eval answers cite).
"""

import pytest

from semigraph.retrieval.answerer import CITE_RE, build_blocks, sources_from_context
from semigraph.retrieval.verify import answer_checks, verify_answer
from test_verify_removal_negation import CONTEXTS as BASE_CONTEXTS
from test_verify_removal_negation import IDS, NEW_ITEM, OLD, item, pair, passage, retrieval, t


def new_reworded_context():
    items = [item("reworded", "New wording", older=[IDS["K"]], newer=[IDS["NEWREW"]]), item("new", "Sovereign AI", newer=[NEW_ITEM])]
    return build_blocks(retrieval(temporal=items, temporal_pairs=[pair(totals={"removed": 0, "new": 1, "reworded": 1})]))


def unsettled_only_context():
    items = [item("unsettled", "Export licensing risk", [IDS["U"]])]
    return build_blocks(retrieval(temporal=items, temporal_pairs=[pair(totals={"removed": 0, "unsettled": 1, "new": 0, "reworded": 0})]))


def real_ids_context():
    ids = [f"{OLD}:I.1A:{n}" for n in ("0238", "0256", "0257", "0258", "0259")]
    passages = [passage("removed", "UVEU sentence.", chunk_ids=ids)]
    return build_blocks(retrieval(temporal_pairs=[pair(passage_totals={"removed": 1, "added": 0, "reworded": 0})],
                                  temporal_passages=passages))


CONTEXTS = {**BASE_CONTEXTS, "new_reworded": new_reworded_context, "unsettled_only": unsettled_only_context,
            "real": real_ids_context}


def claims(context, text):
    _, ctx, valid = CONTEXTS[context]()
    sources = sources_from_context(ctx) if context != "plain" else {}
    return answer_checks(text, set(CITE_RE.findall(text)), valid, ctx, sources=sources).removal_claims



# honest text the check must NOT flag
PASS_CASES = [(family, context, t(text)) for family, context, texts in [
    ("claim-then-restatement-with-the-id-in-a-later-clause", "removal", [
        ("The Hong Kong transition risk no longer appears as a separate risk factor, which means the text "
            "check found no matching text for it in the newer filing [{R}]."),
        ("The Hong Kong transition risk no longer appears as a separate risk factor, and the text check found "
            "no matching text for it in the newer filing [{R}]."),
        ("The NAC sentence's wording was not found in the newer filing, which means the text check found no "
            "matching text for it [{P}]."),
        ("The text check found no matching text in the newer filing for the Hong Kong transition risk, so it "
            "no longer appears as a separate risk factor [{R}]."),
        ("- Hong Kong transition risk: the text check found no matching text in the newer filing, and it no "
            "longer appears as a separate risk factor [{R}]"),
    ]),
    ("claim-then-restatement-with-the-id-in-a-later-clause", "real", [
        ("The UVEU sentence's wording was not found in the newer filing, which is why it is listed among "
            "passages whose wording was not found in the newer filing [0001045810-25-000023:I.1A:0257]."),
    ]),
    ("claim-then-restatement-with-the-id-in-a-later-clause", "removal", [
        ("The text check found no matching text in the newer filing for one older risk factor, the Hong Kong "
            "transition risk, which no longer appears as a separate risk factor [{R}]."),
    ]),
    ("exception-word-before-a-none-quantifier-excepts-a-non-removal", "none", [
        "Apart from rewording, no risk factor was removed.",
        "Other than the reworded items, nothing was dropped.",
        "Other than rewording, no risk factors were removed or dropped.",
        "Except for wording changes, no risk factor was dropped.",
        "Aside from passage-level wording differences, no risk factor was removed.",
        "Besides the rewording, nothing was removed from the risk factors section.",
    ]),
    ("exception-word-before-a-none-quantifier-excepts-a-non-removal", "new_reworded", [
        "Apart from one reworded risk factor [{K}], no risk factor was removed between these two 10-Ks.",
    ]),
    ("none-quantified-subject-with-an-and-inside-its-noun-phrase", "removal", [
        "None of the export and supply risk factors were removed [{K}].",
    ]),
    ("none-quantified-subject-with-an-and-inside-its-noun-phrase", "none", [
        "None of the export and supply risk factors were removed.",
        "No risk factors about export controls and China were removed.",
        "No older risk factor on tariffs and trade was found to no longer appear as a separate risk factor.",
    ]),
    ("abbreviation-period-read-as-a-sentence-end", "none", [
        "No risk factor in the 10-K of Micron Technology, Inc. was removed.",
        "None of the risk factors of Advanced Micro Devices, Inc. were dropped between these two 10-Ks.",
    ]),
    ("comma-list-without-an-oxford-comma", "none", [
        "None of Nvidia, AMD or Micron dropped a risk factor, according to the text check.",
    ]),
    ("negated-noun-phrase-with-an-and-inside-it", "removal", [
        "The comparison did not find that the export and licensing risks were dropped [{K}].",
        "The check did not show export and supply risk factors being removed [{K}].",
        "The text check does not show the export and China risk factors as removed [{K}].",
    ]),
    ("negated-noun-phrase-with-an-and-inside-it", "none", [
        "Neither the export risk factor nor the supply and demand risk factor was dropped.",
    ]),
    ("mid-sentence-without-or-rather-than-reaches-eight-words", "none", [
        "The comparison shows rewording of existing risk factors rather than any risk factor being dropped.",
    ]),
    ("mid-sentence-without-or-rather-than-reaches-eight-words", "removal", [
        "The newer filing rewords the export risk factor without the risk itself being removed [{K}].",
        "The export risk factor was reworded rather than having its disclosure removed [{K}].",
    ]),
    ("mid-sentence-without-or-rather-than-reaches-eight-words", "none", [
        "The changes are rewordings rather than risk factors that were removed.",
    ]),
    ("removed-list-heading-followed-by-none-found-prose", "none", [
        "### Passages whose wording was not found in the newer filing\nThis comparison shows none.",
        ("### Passages whose wording was not found in the newer filing\nThe text check did not list any "
            "passages in this category."),
        "### No longer appears as a separate risk factor\nThis comparison shows none found.",
        "### No longer appears as a separate risk factor\nThis list is empty for this comparison.",
        "### No longer appears as a separate risk factor\nThere are none for this comparison.",
        "### No longer appears as a separate risk factor\nThe automated text comparison found none.",
        "### No longer appears as a separate risk factor\nFor this comparison, the text check found none.",
        "### No longer appears as a separate risk factor\nThe text check did not identify any.",
        ("### No longer appears as a separate risk factor\nThe text check did not find any older risk factor "
            "without matching text in the newer filing."),
        "### No longer appears as a separate risk factor\nNvidia's comparison shows none found.",
        "**No longer appears as a separate risk factor**\nThis comparison shows none.",
    ]),
    ("a-complete-honest-answer-built-from-the-templates", "none", [
        ("No. Based on the automated text comparison between Nvidia's 10-K for the fiscal year ended January "
            "26, 2025 and its 10-K for the fiscal year ended January 25, 2026, the text check found no risk "
            "factor that no longer appears as a separate risk factor.\n\n### No longer appears as a separate risk "
            "factor\nThis comparison shows none found.\n\n### Passages whose wording was not found in the newer "
            "filing\nThe text check found none for this comparison.\n\nIn short, the evidence does not show that any "
            "risk factor from the fiscal year ended January 26, 2025 10-K was removed."),
    ]),
    ("negation-longer-than-eight-words-with-the-required-naming", "none", [
        ("The text check does not show that any of the risk factors in Nvidia's fiscal year ended January 26, "
            "2025 10-K were removed."),
        ("The comparison does not indicate that any risk factor from the fiscal year ended January 26, 2025 "
            "10-K was dropped."),
        ("The comparison does not show the export risk factor from the fiscal year ended January 26, 2025 10-K "
            "being dropped."),
        ("The comparison does not show that any risk factor from Nvidia's 10-K filed 2025-02-26 (accession "
            "0001045810-25-000023) was removed."),
        "It is not the case that the risk factor you asked about was removed between the two 10-Ks.",
    ]),
    ("label-line-with-a-none-statement-in-other-words", "none", [
        "- No longer appears as a separate risk factor: no risk factors listed.",
        "- No longer appears as a separate risk factor: no items.",
        "**No longer appears as a separate risk factor:** no risk factors were listed.",
        "No longer appears as a separate risk factor: this comparison lists none.",
        "No longer appears as a separate risk factor: there are none.",
        "Passages whose wording was not found in the newer filing: this comparison lists none.",
        "Regarding risk factors that no longer appear as a separate risk factor: the text check found none.",
    ]),
    ("paraphrased-heading-followed-by-none-found-prose", "none", [
        "### Removed risk factors\nNone found.",
        "### Dropped risk factors\nThe text check found none.",
        "**Removed risk factors**\nNone.",
        "### Risk factors that no longer appear as a separate risk factor\nNone found.",
        "### No longer appear as separate risk factors\nNone found.",
        "### No longer appearing as a separate risk factor\nNone found.",
        "### Removed risk factors (no longer appears as a separate risk factor)\nNone found.",
    ]),
    ("label-line-with-a-none-statement-in-other-words", "none", [
        "- Removed risk factors: the text check found none.",
        "**No longer appearing as separate risk factors:** the text check found none.",
    ]),
    ("quoted-label-in-lower-case-or-with-a-long-tail", "none", [
        "The \"no longer appears as a separate risk factor\" list is empty for this comparison.",
        "Under \"no longer appears as a separate risk factor\", the comparison shows none found.",
    ]),
    ("label-line-with-a-none-statement-in-other-words", "none", [
        "The list of removed risk factors has no entries.",
    ]),
    ("table-header-followed-by-a-none-row", "none", [
        "| No longer appears as a separate risk factor | Count |\n|---|---|\n| None found | 0 |",
        "| No longer appears as a separate risk factor |\n|---|\n| None found |",
    ]),
    ("denial-adjective-or-contrast-phrase", "removal", [
        "It is incorrect to say that Nvidia removed this risk factor [{K}].",
        "It would be wrong to conclude that Nvidia dropped the export risk factor [{K}].",
        "It would be inaccurate to describe the export risk factor as removed [{K}].",
        "That is different from the risk factor being removed [{K}].",
        "Unlike a removed risk factor, a reworded one is still disclosed [{K}].",
    ]),
    ("list-mention-negated-by-absent-excluded-or-rather-than", "removal", [
        "The export risk factor is absent from the \"No longer appears as a separate risk factor\" list [{K}].",
        ("The export risk factor is missing from the \"No longer appears as a separate risk factor\" list and is "
            "listed as reworded instead [{K}] [{NEWREW}]."),
        ("The export risk factor is excluded from the \"No longer appears as a separate risk factor\" list "
            "because it was reworded [{K}]."),
        ("Rather than being listed under \"No longer appears as a separate risk factor\", the export risk factor "
            "is listed as reworded [{K}]."),
    ]),
    ("refusal-with-a-whether-clause-or-unknown", "unsettled_only", [
        "It remains unverified whether the Export licensing risk was removed [{U}].",
        "Whether the Export licensing risk was removed could not be verified [{U}].",
        ("Whether it was removed or absorbed into another risk factor is something the text check could not "
            "verify [{U}]."),
    ]),
    ("refusal-with-a-whether-clause-or-unknown", "none", [
        ("Whether any risk factor was removed between those two filings cannot be determined from this "
            "context."),
        ("Since no comparison covers the fiscal year ended January 28, 2024 10-K, whether Nvidia removed any "
            "risk factors in that year cannot be determined."),
        ("Which risk factors, if any, were removed between the FY2023 and FY2024 10-Ks is not covered by the "
            "context."),
        "No comparison is available for those years, so it is unknown whether any risk factors were dropped.",
        "It remains an open question whether Nvidia dropped any risk factor between those filings.",
        "The question of whether any risk factor was removed cannot be answered from the context.",
        ("The provided comparison does not cover those fiscal years, so any claim that Nvidia dropped a risk "
            "factor there would be unsupported."),
    ]),
    ("refusal-with-a-whether-clause-or-unknown", "plain", [
        ("No comparison is available: the context has no REMOVED / ADDED / REWORDED data for Micron, so "
            "whether any risk factor was removed is unknown."),
    ]),
    ("block-title-written-with-commas", "new_reworded", [
        ("### Removed, added and reworded risk factors\n- No longer appears as a separate risk factor: none "
            "found.\n- No matching risk factor found in the earlier filing: Sovereign AI [{N}]\n- Reworded: New "
            "wording [{K}] [{NEWREW}]"),
        ("**Risk factors removed, added or reworded:**\n- Sovereign AI: no matching risk factor was found in "
            "the earlier filing (it is new, or a restructured older risk factor) [{N}]\n- New wording: reworded, "
            "still disclosed [{K}] [{NEWREW}]"),
    ]),
    ("relative-aside-splits-the-negated-clause", "removal", [
        ("The comparison does not show that Nvidia, which reworded at least 1 risk factor, removed any of them "
            "[{K}]."),
    ]),
    ("abbreviation-period-read-as-a-sentence-end", "none", [
        ("The text check did not identify any risk factor in the 10-K of Micron Technology, Inc. as no longer "
            "appearing as a separate risk factor."),
        "The comparison did not show any risk factor of NVIDIA Corp. as removed.",
        "The text check did not flag risk factors on tariffs, sanctions, etc. as removed.",
        "No risk factor from the fiscal year ended Jan. 26, 2025 10-K was removed.",
    ]),
    ("comma-list-without-an-oxford-comma", "none", [
        "The comparison does not show that Nvidia, AMD or Micron removed any risk factor.",
    ]),
    ("omitted-from-the-context-is-not-a-disclosure-removal", "new_reworded", [
        ("Only 1 of the reworded risk factors is shown; the remaining items are omitted from the context "
            "[{K}]."),
    ]),
    ("adverb-since-once-so-after-the-negator", "removal", [
        "The Hong Kong risk factor has not since been removed [{K}].",
    ]),
    ("adverb-since-once-so-after-the-negator", "none", [
        "The export risk factor has not since been removed.",
    ]),
    ("adverb-since-once-so-after-the-negator", "removal", [
        "The export risk factor has never once been dropped [{K}].",
        "The export risk factor was not so much removed as reworded [{K}].",
    ]),
    ("adverb-since-once-so-after-the-negator", "none", [
        "Nvidia has not once dropped a risk factor.",
    ]),
    ("comma-list-without-an-oxford-comma", "none", [
        "No risk factor, passage or paragraph was removed.",
    ]),
    ("none-quantified-subject-with-an-and-inside-its-noun-phrase", "none", [
        "None of the risk factors and passages were removed.",
    ]),
    ("exception-word-before-a-none-quantifier-excepts-a-non-removal", "none", [
        "Apart from some rewording, no risk factor was removed.",
        "Other than the rewording discussed above, no risk factor was removed.",
        "Besides rewording, the comparison found no risk factor that was removed.",
    ]),
    ("abbreviation-period-read-as-a-sentence-end", "none", [
        "No risk factor of Nvidia Corp. was dropped.",
        "No risk factor in the older vs. newer 10-K was removed.",
        "No export, tax, etc. risk factor was removed.",
    ]),
    ("negated-noun-phrase-with-an-and-inside-it", "none", [
        "It does not mean the export and Hong Kong risks were removed.",
        "The check did not find that risk factors and passages were removed.",
        "Neither the export risk factor nor the Hong Kong and Taiwan risk factors were removed.",
    ]),
    ("none-quantifier-with-a-relative-pronoun-far-from-the-verb", "none", [
        ("The text check found no risk factor from the fiscal year ended December 28, 2024 filing that "
            "Nvidia's newer annual report on Form 10-K dropped."),
    ]),
    ("mid-sentence-without-or-rather-than-reaches-eight-words", "removal", [
        "The export risk factor was reworded without the company actually removing it [{K}].",
    ]),
    ("adverb-since-once-so-after-the-negator", "none", [
        "The comparison has not so far shown that any risk factor was removed.",
    ]),
    ("quoted-label-in-lower-case-or-with-a-long-tail", "none", [
        ("The \"No longer appears as a separate risk factor\" list for the fiscal 2025 and fiscal 2026 10-K "
            "filings shows none found."),
        ("The \"No longer appears as a separate risk factor\" list of the automated text comparison between "
            "Nvidia's two most recent annual reports shows none found."),
    ]),
    ("list-mention-negated-by-absent-excluded-or-rather-than", "removal", [
        "The export risk factor is absent from the \"No longer appears as a separate risk factor\" list [{K}].",
    ]),
    ("quoted-label-in-lower-case-or-with-a-long-tail", "none", [
        "The \"no longer appears as a separate risk factor\" list shows none found for this comparison.",
        "The \"no longer appears as a separate risk factor\" list is empty.",
        "The \"passages whose wording was not found in the newer filing\" list shows none found.",
    ]),
    ("claim-line-with-a-quoted-label-and-sub-bullets", "removal", [
        ("- The text check found no risk factor that no longer appears as a separate risk factor: the \"No "
            "longer appears as a separate risk factor\" list shows none found for this comparison.\n  - The "
            "reworded risk factor is still disclosed [{K}]"),
        ("The \"No longer appears as a separate risk factor\" list shows none found; the reworded risk factors "
            "are:\n- New wording [{K}]"),
        ("The \"No longer appears as a separate risk factor\" list shows none found. These were only reworded:\n- "
            "New wording [{K}]"),
    ]),
    ("independent-probe-batch-1-honest-text", "none", [
        "No risk factors were removed.",
        "Nvidia did not remove any risk factors.",
        "The comparison found no removed risk factors.",
        "There were no removals of risk factors.",
        "No risk factor was dropped between the two filings.",
        "The check did not find that any risk factor was removed.",
    ]),
    ("independent-probe-batch-1-honest-text", "removal", [
        "Nvidia has not dropped the indebtedness risk factor [{K}].",
        "The indebtedness risk factor was reworded, not removed [{K}].",
        "The indebtedness risk factor was reworded rather than removed [{K}].",
        "The indebtedness risk factor still appears in the newer filing, so it was not removed [{K}].",
    ]),
    ("independent-probe-batch-1-honest-text", "none", [
        "None of the 24 older risk factors were removed.",
        "Zero risk factors were removed.",
        "0 risk factors were removed.",
        "There is no evidence that Nvidia removed any risk factor.",
    ]),
    ("independent-probe-batch-1-honest-text", "removal", [
        "The text check does not indicate that the risk factor was removed [{K}].",
    ]),
    ("independent-probe-batch-1-honest-text", "none", [
        "Nvidia neither removed nor added risk factors.",
        "The comparison shows no risk factor being dropped.",
    ]),
    ("independent-probe-batch-1-honest-text", "removal", [
        "The export risk factor was not deleted [{K}].",
    ]),
    ("independent-probe-batch-1-honest-text", "none", [
        "Nothing was dropped from the risk factors.",
        "No passage was removed.",
        "I cannot say whether Nvidia removed any risk factor.",
        "It is unknown whether the risk factor was removed.",
    ]),
    ("independent-probe-batch-1-honest-text", "unsettled", [
        "The text check could not verify whether the risk factor was removed [{U}].",
    ]),
    ("independent-probe-batch-1-honest-text", "none", [
        "Removed risk factors: none.",
        "The removed list is empty.",
        "The comparison identified no risk factors that were removed.",
        "The comparison identified no risk factor as removed.",
        "The comparison did not identify a single risk factor as removed.",
        "Not a single risk factor was removed.",
        "Not one risk factor was dropped.",
        "Neither risk factor was removed.",
        "Neither the export risk factor nor the Russia risk factor was removed.",
        "No one risk factor was removed.",
        "There are no risk factors that no longer appear as a separate risk factor.",
        "No risk factor no longer appears as a separate risk factor.",
        "The comparison shows that no risk factor no longer appears as a separate risk factor.",
        "The comparison lists no risk factor that no longer appears as a separate risk factor.",
        "Nvidia did not stop disclosing any risk factor.",
        "Nvidia has not stopped disclosing any risk factor.",
        "Nvidia never dropped any risk factor.",
        "Nvidia never removed a risk factor in these filings.",
        "The newer filing does not omit any risk factor.",
        "The newer filing did not eliminate any risk factor.",
        "The newer 10-K contains every risk factor of the older one; none was removed.",
        "Every risk factor of the older 10-K still appears; none was dropped.",
        "All risk factors remain; none were removed.",
        "Nothing suggests that any risk factor was removed.",
        "Nothing in the comparison suggests a risk factor was dropped.",
        "Nothing in the comparison shows that a risk factor was dropped.",
        "The comparison contains nothing about a risk factor being dropped.",
        "The comparison provides no basis to say a risk factor was removed.",
        "There is no basis for saying that Nvidia removed a risk factor.",
        "The text check gives no reason to think a risk factor was removed.",
        "I found no sign that any risk factor was deleted.",
        "The filings show no sign of any risk factor being eliminated.",
        "The filings show no indication that Nvidia dropped a risk factor.",
        "Neither filing indicates that a risk factor was removed.",
        "Neither comparison shows a risk factor as removed.",
        "Neither the text check nor the graph shows that a risk factor was removed.",
        "Neither the older nor the newer wording shows a risk factor being dropped.",
        "Neither list shows a removed risk factor.",
    ]),
    ("independent-probe-batch-1-honest-text", "removal", [
        "The Taiwan risk factor was not removed; it was reworded [{K}].",
        "The Taiwan risk factor was not removed but reworded [{K}].",
        "The Taiwan risk factor was not dropped, only reworded [{K}].",
        "The Taiwan risk factor was not dropped or removed [{K}].",
        "The Taiwan risk factor was not dropped, deleted or removed [{K}].",
        "The Taiwan risk factor was neither dropped nor removed [{K}].",
        "The Taiwan risk factor was neither removed nor deleted [{K}].",
        "The Taiwan risk factor was neither dropped, deleted, nor removed [{K}].",
        "The Taiwan risk factor has not been removed; it is reworded [{K}].",
        "The Taiwan risk factor has not been removed and remains disclosed [{K}].",
        "The Taiwan risk factor remains disclosed and has not been removed [{K}].",
        "The Taiwan risk factor remains and was never removed [{K}].",
        "The Taiwan risk factor was never dropped and is still disclosed [{K}].",
        "Nvidia did not drop the Taiwan risk factor; it reworded it [{K}].",
        "Nvidia did not drop the Taiwan risk factor, it only reworded it [{K}].",
        "Nvidia did not remove the Taiwan risk factor, and the Taiwan risk factor was reworded [{K}].",
        "Nvidia did not drop or remove the Taiwan risk factor [{K}].",
        "Nvidia did not drop, remove or delete the Taiwan risk factor [{K}].",
        "Nvidia did not remove, drop, or delete the Taiwan risk factor [{K}].",
        "Nvidia did not remove the Taiwan risk factor, nor did it drop the China risk factor [{K}].",
        "Nvidia did not remove the Taiwan risk factor and did not drop the China risk factor [{K}].",
        "Nvidia did not remove the Taiwan risk factor and did not delete the China risk factor [{K}].",
        "Nvidia did not remove the Taiwan risk factor and neither did it delete the China risk factor [{K}].",
        ("Nvidia did not remove the Taiwan risk factor, and it did not drop the China risk factor either "
            "[{K}]."),
        "Nvidia did not remove the Taiwan risk factor; it also did not drop the China risk factor [{K}].",
        "The check did not find that Nvidia removed the Taiwan risk factor [{K}].",
        "The check did not find that the Taiwan risk factor was removed [{K}].",
        "The check does not show that the Taiwan risk factor was dropped [{K}].",
        "The comparison does not suggest that Nvidia deleted the Taiwan risk factor [{K}].",
        "The comparison provides no support for the claim that Nvidia removed the Taiwan risk factor [{K}].",
        "There is no support for the claim that the Taiwan risk factor was removed [{K}].",
        "The claim that the Taiwan risk factor was removed is not supported [{K}].",
        "The claim that Nvidia removed the Taiwan risk factor is unsupported [{K}].",
        "I cannot confirm that the Taiwan risk factor was removed [{K}].",
        "We cannot conclude that Nvidia dropped the Taiwan risk factor [{K}].",
        "One cannot conclude from this comparison that Nvidia dropped the Taiwan risk factor [{K}].",
        "It cannot be concluded that the Taiwan risk factor was removed [{K}].",
        "It would be a mistake to say that the Taiwan risk factor was removed [{K}].",
        "It would be misleading to say Nvidia deleted the Taiwan risk factor [{K}].",
        "It would be premature to conclude that Nvidia dropped the Taiwan risk factor [{K}].",
        "It is not accurate to say that the Taiwan risk factor was removed [{K}].",
        "It is not correct to say that Nvidia removed the Taiwan risk factor [{K}].",
        "It is not true that the Taiwan risk factor was removed [{K}].",
        "It isn't true that Nvidia dropped the Taiwan risk factor [{K}].",
        "This does not mean the Taiwan risk factor was removed [{K}].",
        "This doesn't mean Nvidia removed the Taiwan risk factor [{K}].",
        "That does not imply that Nvidia dropped the Taiwan risk factor [{K}].",
        "A change in wording does not mean that a risk factor was dropped [{K}].",
        "A change in wording is not the same as a risk factor being dropped [{K}].",
        "Rewording is not removal, and the Taiwan risk factor was reworded [{K}].",
        "Rewording is not the same as removing a risk factor [{K}].",
        "Reworded does not mean removed [{K}].",
        "Reworded is not removed [{K}].",
        "Being reworded is different from being removed [{K}].",
        "The Taiwan risk factor was reworded, which is different from being removed [{K}].",
        "The Taiwan risk factor was reworded, which is not the same as removed [{K}].",
        "The Taiwan risk factor was reworded, which does not mean it was removed [{K}].",
        "The Taiwan risk factor was reworded, which does not imply that it was dropped [{K}].",
        "The Taiwan risk factor was reworded; that does not mean it was removed [{K}].",
        "The Taiwan risk factor was reworded. That does not mean it was removed [{K}].",
        "The Taiwan risk factor was reworded (not removed) [{K}].",
        "The Taiwan risk factor was reworded (it was not removed) [{K}].",
        "The Taiwan risk factor was reworded — not removed [{K}].",
        "The Taiwan risk factor was reworded - not removed [{K}].",
        "The Taiwan risk factor was reworded, and not removed [{K}].",
        "The Taiwan risk factor was reworded and not removed [{K}].",
        "The Taiwan risk factor was reworded and never removed [{K}].",
        "The Taiwan risk factor was reworded and is not among the removed ones [{K}].",
        "The Taiwan risk factor is not listed as removed [{K}].",
        "The Taiwan risk factor is not listed among the removed risk factors [{K}].",
        "The Taiwan risk factor is not on the list of removed risk factors [{K}].",
        ("The Taiwan risk factor is not among the risk factors that no longer appear as a separate risk factor "
            "[{K}]."),
        "The Taiwan risk factor is not one of the risk factors that were removed [{K}].",
        "The Taiwan risk factor was not one of those removed [{K}].",
        "The Taiwan risk factor was not among the removed [{K}].",
        "The Taiwan risk factor did not disappear [{K}].",
        "The Taiwan risk factor did not vanish; it was reworded [{K}].",
        "The Taiwan risk factor was not lost [{K}].",
    ]),
    ("independent-probe-batch-2-honest-text", "none", [
        "The text check did not flag any risk factor as removed.",
        "The text check did not flag any risk factor that was removed.",
        "No risk factor was flagged as removed.",
        "The comparison flagged no removals.",
        "Nothing was flagged as removed.",
        "The summary does not highlight any removed risk factors.",
    ]),
    ("independent-probe-batch-2-honest-text", "removal", [
        "The risk factor was not identified as removed [{K}].",
    ]),
    ("independent-probe-batch-2-honest-text", "none", [
        "Nothing is flagged as removed.",
        "The comparison did not mark any risk factor as removed.",
    ]),
    ("independent-probe-batch-2-honest-text", "removal", [
        "The export risk factor is described as reworded, not as removed [{K}].",
    ]),
    ("independent-probe-batch-2-honest-text", "none", [
        "Whether Nvidia removed the export risk factor cannot be determined from the context.",
        "The question is whether any risk factor was removed, which the context does not answer.",
    ]),
    ("independent-probe-batch-2-honest-text", "unsettled", [
        "It is unclear whether or not the export risk factor was removed [{U}].",
    ]),
    ("independent-probe-batch-2-honest-text", "removal", [
        "The Russia risk factor is not among the risk factors that were removed [{K}].",
    ]),
    ("independent-probe-batch-2-honest-text", "none", [
        "1. No risk factor was removed.",
        "- No risk factor was removed.",
        "**No risk factors were removed.**",
        "> No risk factor was removed.",
        ("No risk factor in the annual report on Form 10-K that Nvidia filed with the SEC for the fiscal year "
            "ended January 26, 2025 was removed."),
        "None of the risk factors that Nvidia disclosed in its annual report for fiscal 2025 were removed.",
        "### No longer appears as a separate risk factor\nNo entries.",
        "### Removed risk factors\nNo risk factors were removed.",
        "### Removed risk factors\nThe list is empty for this comparison.",
        "### Removed risk factors\nThe check found no removed risk factors.",
        "None of the risk factors of Meta Platforms, Inc. or Advanced Micro Devices, Inc. were dropped.",
        "Nvidia did not add, reword or drop any risk factor.",
        "No risk factor was removed in Jan. 2026.",
    ]),
    ("independent-probe-batch-2-honest-text", "removal", [
        "| Taiwan | Not removed [{K}] |",
        "- Taiwan risk factor: still disclosed [{K}]",
        "It was not removed [{K}].",
        "It was reworded, not removed [{K}].",
        "Nvidia did not remove it [{K}].",
        "They were reworded and none were removed [{K}].",
        "Both were reworded, not removed [{K}].",
        "It would be incorrect to say that Nvidia removed the export risk factor [{K}].",
        "The Taiwan risk factor is not on the \"No longer appears as a separate risk factor\" list [{K}].",
    ]),
    ("independent-probe-batch-2-honest-text", "none", [
        "No passage had wording that was not found in the newer filing.",
        "Every passage was found in the newer filing.",
        ("Based on the text comparison, no risk factors were found to have been removed between the fiscal "
            "year ended January 26, 2025 10-K and the fiscal year ended January 25, 2026 10-K."),
        ("The text comparison found that no risk factor from the fiscal year ended January 26, 2025 10-K no "
            "longer appears as a separate risk factor in the fiscal year ended January 25, 2026 10-K."),
        ("The text comparison did not identify any risk factor from the fiscal year ended January 26, 2025 "
            "10-K as removed."),
        ("The text comparison did not show that any risk factor from the fiscal year ended January 26, 2025 "
            "10-K was removed."),
        ("The text comparison did not show any evidence that any risk factor from the fiscal year ended "
            "January 26, 2025 10-K was removed."),
        ("There is no evidence in the text comparison that any risk factor from the fiscal year ended January "
            "26, 2025 10-K was removed."),
        ("The comparison of the fiscal year ended January 26, 2025 10-K with the fiscal year ended January 25, "
            "2026 10-K shows no removed risk factors."),
        "The comparison of the two 10-Ks shows that no risk factor was removed.",
        "The comparison of the two 10-Ks shows that none of the older risk factors was removed.",
        "The two 10-Ks share all risk factors; none was removed.",
        "Nvidia removed none of its risk factors.",
        "Nvidia dropped no risk factors.",
        "Nvidia deleted nothing from its risk factors.",
        "Nvidia eliminated no disclosures.",
        "Nvidia removed zero risk factors.",
        "Nvidia removed 0 risk factors.",
        "Nvidia removed not a single risk factor.",
        "Nvidia did not drop a single risk factor.",
        "Nvidia did not remove even one risk factor.",
        "Nvidia did not delete any of the risk factors from the older filing.",
        "Nvidia did not delete any of its older risk factors in the newer filing.",
        "Nvidia did not, in the newer filing, delete any of its older risk factors.",
        "Nvidia did not, according to the text check, delete any of its older risk factors.",
        "Nvidia did not, as far as the text check can tell, delete any of its older risk factors.",
        "Based on the text check, Nvidia did not delete any of its older risk factors.",
        "Based on the text check, there is no risk factor that Nvidia deleted.",
        "Based on the text check, there is no risk factor that was deleted.",
        "Based on the text check, there are no deleted risk factors.",
        "Based on the text check, no risk factor appears to have been deleted.",
        "Based on the text check, no risk factor seems to have been removed.",
        "Based on the text check, it does not appear that any risk factor was removed.",
        "Based on the text check, it does not seem that any risk factor was dropped.",
        "Based on the text check, it is not the case that any risk factor was removed.",
        "Based on the text check, it is not true that any risk factor was removed.",
        "Based on the text check, it is false that any risk factor was removed.",
        "Based on the text check, the claim that any risk factor was removed is unsupported.",
        "The text check gives no support to the claim that any risk factor was removed.",
        "The text check provides no evidence that any risk factor has been removed.",
        "The text check provides no evidence of a risk factor having been removed.",
        "The text check provides no evidence of any risk factor removal.",
        "The text check provides no evidence of removal of any risk factor.",
        "The text check found no removal of any risk factor.",
        "The text check found no removals.",
        "The text check found no risk factor removals.",
        "The text check found no risk factor removed from the older filing.",
        "The text check found no risk factor that was removed from the older filing.",
        "The text check identified no risk factors that were dropped.",
        "The text check identified no dropped risk factors.",
        "No dropped risk factors were identified.",
        "No removed risk factors were identified.",
        "There are no removed risk factors.",
        "There are no removed risk factors in this comparison.",
        "Removed risk factors: 0.",
        "Removed risk factors: none identified.",
        "Risk factors removed: none.",
        "Risk factors removed: 0",
        "Risk factors removed - none.",
        "Risk factors dropped: none found.",
        "Dropped: none.",
        "**Dropped:** none.",
        "**Removed:** none found.",
        "**Removed:** 0",
        "- Removed: none",
        "- Removed risk factors: none",
        "- Removed risk factors: 0",
        "- Removed risk factors: n/a",
    ]),
    ("independent-probe-batch-3-honest-text", "none", [
        "Nvidia removed nothing.",
        "Nvidia removed nothing from the risk factors section.",
        "Nvidia removed no risk factors from the older filing.",
        "Nvidia removed no risk factors, according to the text check.",
        "Nvidia removed none of its 24 risk factors.",
        "Nvidia dropped none of the risk factors that it disclosed in the older filing.",
        "Nvidia dropped none of its risk factors, and the text check confirms it.",
        "Nvidia dropped none of its risk factors; the text check lists none.",
        "Nvidia removed no risk factors, and it did not add any either.",
        "Nvidia removed no risk factors and added none.",
        "Nvidia added no risk factors and removed none.",
        "Nvidia added no risk factors and dropped none.",
        "Nvidia added no new risk factors and removed no existing ones.",
        "Nvidia added no risk factors and did not remove any.",
        "Nvidia added no risk factors, and it removed none of the old ones.",
        "Nvidia added nothing and removed nothing.",
        "Nvidia added nothing, removed nothing and dropped nothing.",
        "Nvidia neither added nor removed risk factors.",
        "Nvidia neither added nor removed any risk factor.",
        "Nvidia neither added, removed, nor reworded any risk factor.",
        "The newer 10-K neither adds nor drops any risk factor.",
        "The text check found nothing added and nothing removed.",
        "The text check found nothing added or removed.",
        "The text check found nothing to report on removals.",
        "There was nothing to remove.",
        "There were no removals and no additions.",
        "There were no additions or removals.",
        "There were no additions, removals, or rewordings.",
        "There were no additions, no removals and no rewordings.",
        "There are no removed risk factors, added risk factors or reworded risk factors.",
        "No risk factor was added, removed or reworded.",
        "No risk factors were added, removed, or reworded.",
        "No risk factors were added or dropped.",
        "No risk factor was added or deleted.",
        "No risk factor was added, and none was dropped.",
        "No risk factor was added; none was dropped.",
        "No risk factor was added. None was dropped.",
        "No risk factor was added and none was dropped.",
        "No risk factors were added and no risk factors were removed.",
        "No risk factors were added and none were removed.",
        "None were added and none were removed.",
        "None was added, none was removed.",
        "None of the risk factors were added or removed.",
        "None of the risk factors was added, removed or reworded.",
        "None of them was removed.",
        "None of these were dropped.",
        "None of those was deleted.",
        "None of the older filing's risk factors were deleted.",
        "None of the older filing's 24 risk factors was omitted.",
        "None of the sentences from the older filing were removed.",
        "None of the passages was removed.",
        "None of the paragraphs were dropped.",
        "None of the statements in the older filing was deleted.",
        "No sentence was removed.",
        "No statement was dropped.",
        "No paragraph was deleted.",
        "No disclosure was eliminated.",
        "No disclosure was omitted from the newer filing.",
        "No disclosure was discontinued.",
        "No section was removed.",
        "No item was removed.",
        "No items were removed.",
        "No entries were removed.",
        "No content was removed.",
        "No language was dropped.",
        "No wording was removed.",
        "No text was deleted.",
        "No text was removed from the older filing.",
    ]),
    ("independent-probe-batch-3-honest-text", "removal", [
        "The Taiwan risk factor was reworded, and no risk factor was removed [{K}].",
        "The Taiwan risk factor was reworded; no risk factor was removed [{K}].",
        "The Taiwan risk factor was reworded. No risk factor was removed [{K}].",
        "The Taiwan risk factor was reworded, but not removed [{K}].",
        "The Taiwan risk factor was reworded but not removed [{K}].",
        "The Taiwan risk factor was reworded but was not removed [{K}].",
        "The Taiwan risk factor was reworded but has not been removed [{K}].",
        "The Taiwan risk factor was reworded, and it has not been removed [{K}].",
        "The Taiwan risk factor was reworded, and it was not removed [{K}].",
        "The Taiwan risk factor was reworded, and it was never removed [{K}].",
        "The Taiwan risk factor was reworded, and it is still disclosed; it was not dropped [{K}].",
        "The Taiwan risk factor was reworded, and it was not dropped or deleted [{K}].",
        "The Taiwan risk factor was reworded and not dropped, deleted, or removed [{K}].",
        "The Taiwan risk factor was reworded, so it was not removed [{K}].",
        "The Taiwan risk factor was reworded, so no removal occurred [{K}].",
        "The Taiwan risk factor was reworded, so there was no removal [{K}].",
        "The Taiwan risk factor was reworded, so there was no removal of it [{K}].",
        "The Taiwan risk factor was reworded, so nothing was removed [{K}].",
        "The Taiwan risk factor was reworded, so none of it was removed [{K}].",
        "The Taiwan risk factor was reworded, hence it was not removed [{K}].",
        "The Taiwan risk factor was reworded, which means it was not removed [{K}].",
        "The Taiwan risk factor was reworded, which means that it was not dropped [{K}].",
        "The Taiwan risk factor was reworded, meaning it was not removed [{K}].",
        "The Taiwan risk factor was reworded, i.e. not removed [{K}].",
        "The Taiwan risk factor was reworded, i.e., it was not removed [{K}].",
        "The Taiwan risk factor was reworded, not dropped [{K}].",
        "The Taiwan risk factor was reworded, not eliminated [{K}].",
        "The Taiwan risk factor was reworded, not deleted [{K}].",
        "The Taiwan risk factor was reworded, not omitted [{K}].",
        "The Taiwan risk factor was reworded, not discontinued [{K}].",
        "The Taiwan risk factor was reworded, not removed or dropped [{K}].",
        "The Taiwan risk factor was reworded, neither removed nor dropped [{K}].",
        "The Taiwan risk factor was reworded, and neither removed nor dropped [{K}].",
        "The Taiwan risk factor was reworded, never removed [{K}].",
        "The Taiwan risk factor was reworded and never removed [{K}].",
        "The Taiwan risk factor was reworded and never dropped [{K}].",
        "The Taiwan risk factor was changed, not removed [{K}].",
        "The Taiwan risk factor was changed but not removed [{K}].",
        "The Taiwan risk factor was modified, not eliminated [{K}].",
        "The Taiwan risk factor was updated, not deleted [{K}].",
        "The Taiwan risk factor was revised rather than removed [{K}].",
        "The Taiwan risk factor was revised instead of being dropped [{K}].",
        "The Taiwan risk factor was revised, as opposed to removed [{K}].",
        "The Taiwan risk factor was retained, not removed [{K}].",
        "The Taiwan risk factor was kept, not dropped [{K}].",
        "The Taiwan risk factor remains, it was not removed [{K}].",
        "The Taiwan risk factor remains disclosed; it was not removed [{K}].",
        "The Taiwan risk factor remains disclosed and was not removed [{K}].",
        "The Taiwan risk factor persists and was not dropped [{K}].",
        "The Taiwan risk factor persists, so it was not dropped [{K}].",
        "The Taiwan risk factor persists, meaning it was not deleted [{K}].",
        "The Taiwan risk factor persists; nothing was deleted [{K}].",
        "The Taiwan risk factor persists; none of it was deleted [{K}].",
        "It persists, so it was not deleted [{K}].",
        "It persists and was not deleted [{K}].",
        "It was reworded, so no removal occurred [{K}].",
        "It was reworded; no removal occurred [{K}].",
        "It was reworded and no removal occurred [{K}].",
        "It was reworded. No removal occurred [{K}].",
        "No removal occurred [{K}].",
        "No removal took place [{K}].",
        "No removal was found [{K}].",
        "No removal was identified [{K}].",
        "No removals were identified [{K}].",
        "No removals were found in the comparison [{K}].",
        "No deletion occurred.",
        "No elimination was found.",
        "No omission was found.",
        "No discontinuation was identified.",
    ]),
    ("independent-probe-batch-4-honest-text", "removal", [
        "| Risk factor | Status |\n|---|---|\n| Taiwan | Reworded [{K}] |\n| Export | Reworded [{NEWREW}] |",
        "| Risk factor | Status |\n|---|---|\n| Taiwan | Removed [{R}] |\n| Export | Reworded [{K}] |",
        "| Risk factor | Status |\n|---|---|\n| Export | Reworded [{K}] |\n| Taiwan | Removed [{R}] |",
        "- Taiwan: reworded [{K}]\n- Export: no longer appears as a separate risk factor [{R}]",
        "- Export: no longer appears as a separate risk factor [{R}]\n- Taiwan: reworded [{K}]",
        "- **Export** — no longer appears as a separate risk factor [{R}]\n- **Taiwan** — reworded [{K}]",
    ]),
    ("independent-probe-batch-4-honest-text", "none", [
        "### Summary\nNo risk factors were removed.\n\n### Details\n- Taiwan was reworded [{K}]",
    ]),
    ("independent-probe-batch-4-honest-text", "removal", [
        "### Summary\nNo risk factors were removed.\n\n### Details\n- Taiwan was reworded [{K}]",
        "**Removed:** none\n**Reworded:** Taiwan [{K}]",
        "**Removed:**\n- None\n**Reworded:**\n- Taiwan [{K}]",
        "**Removed:**\n- None\n\n**Reworded:**\n- Taiwan [{K}]",
        "**Removed risk factors:**\n- None found.\n\n**Reworded risk factors:**\n- Taiwan [{K}]",
        "- Removed risk factors:\n  - Taiwan [{R}]\n  - Export [{R}]",
        "Removed risk factors (2):\n1. Taiwan [{R}]\n2. Export [{R}]",
        "**Nvidia removed the Taiwan risk factor [{R}]**",
        "**Nvidia removed the Taiwan risk factor [{R}]**\nThe Export risk factor was reworded [{K}].",
        ("### Removed risk factors\n- Taiwan risk factor [{R}]\n\n### Reworded risk factors\n- Export risk factor "
            "[{K}]"),
        ("### Removed risk factors\n- Taiwan risk factor [{R}]\n### Reworded risk factors\n- Export risk factor "
            "[{K}]"),
        ("### Removed risk factors\nThe Taiwan risk factor [{R}].\n\n### Reworded risk factors\nThe export risk "
            "factor [{K}]."),
        ("The Taiwan risk factor no longer appears as a separate risk factor [{R}]. The export risk factor was "
            "reworded [{K}]."),
        ("The Taiwan risk factor no longer appears as a separate risk factor [{R}], and the export risk factor "
            "was reworded [{K}]."),
        ("The Taiwan risk factor no longer appears as a separate risk factor [{R}]; the export risk factor was "
            "reworded and is still disclosed [{K}]."),
        ("The Taiwan risk factor no longer appears as a separate risk factor [{R}], while the export risk "
            "factor was reworded [{K}]."),
        ("Reworded, still disclosed: the export risk factor [{K}]. No longer appears as a separate risk "
            "factor: the Taiwan risk factor [{R}]."),
        ("The NAC sentence's wording was not found in the newer filing [{P}]; the export risk factor itself "
            "was reworded [{K}]."),
        ("The NAC sentence's wording was not found in the newer filing, but a differently worded version may "
            "exist [{P}]."),
        ("The NAC sentence's wording was not found in the newer filing (a differently worded version of the "
            "same statement may exist) [{P}]."),
        ("The NAC sentence's wording \"was not found\" in the newer filing [{P}], which does not mean the risk "
            "factor was removed."),
        ("The NAC sentence's wording was not found in the newer filing [{P}], but the risk factor was not "
            "removed."),
        "Passages whose wording was not found in the newer filing: the NAC sentence [{P}].",
        "- Passages whose wording was not found in the newer filing: the NAC sentence [{P}]",
        "- The NAC sentence [{P}]: wording not found in the newer filing",
        "- The NAC sentence: wording not found in the newer filing [{P}]",
        ("The Taiwan risk factor no longer appears as a separate risk factor: the text check found no matching "
            "text for it in the newer filing [{R}]."),
        "The Taiwan risk factor [{R}] no longer appears as a separate risk factor.",
        "[{R}] The Taiwan risk factor no longer appears as a separate risk factor.",
        ("The Taiwan risk factor, which no longer appears as a separate risk factor [{R}], may be covered "
            "elsewhere."),
        ("The Taiwan risk factor, which no longer appears as a separate risk factor, may be covered elsewhere "
            "[{R}]."),
        ("Nvidia's Taiwan risk factor (which no longer appears as a separate risk factor) may be covered "
            "elsewhere [{R}]."),
        "Two risk factors no longer appear as a separate risk factor: Taiwan [{R}] and China [{R}].",
    ]),
    ("independent-probe-batch-5-plain-context-honest-text", "plain", [
        "Nvidia stopped selling the A800 in China; it was discontinued in 2023 [{RISK}].",
        "The USG revised the license rules and they were eliminated [{RISK}].",
        "- H20 shipments to China: discontinued [{RISK}]",
        "| Product | Status |\n|---|---|\n| A800 | Discontinued [{RISK}] |",
        "The company discontinued it in fiscal 2024 [{RISK}].",
        "Nvidia removed it from the roadmap [{RISK}].",
        "Both were eliminated by the new rules [{RISK}].",
        "The export license requirement was eliminated, and they resumed shipments [{RISK}].",
        "It was dropped [{RISK}].",
        "- Data center revenue: dropped [{RISK}]",
        "- Gross margin: eliminated [{RISK}]",
        "Removed [{RISK}].",
        "Nvidia's 10-K does not discuss the removal of tariffs on chips [{RISK}].",
        "The filing does not explain the removal of the license requirement [{RISK}].",
        "No tariffs were removed in 2025 [{RISK}].",
        "No export restriction was eliminated [{RISK}].",
        "Nvidia did not explain why the license requirement was eliminated [{RISK}].",
        "The company did not comment on the elimination of the H20 restriction [{RISK}].",
        "The risk factor does not say whether the rule was rescinded or dropped [{RISK}].",
        "The filing says nothing about the rule being removed [{RISK}].",
        "There is no mention of the restriction being lifted or removed [{RISK}].",
        "The risk factor is not affected by the removal of the tariff [{RISK}].",
        "Nvidia did not remove any product from its lineup [{RISK}].",
        "No product was discontinued in the period [{RISK}].",
        "None of the export controls were eliminated [{RISK}].",
        "The filing states that the licenses were not eliminated [{RISK}].",
        "The risk factor that Nvidia did not update was published in the 10-K [{RISK}].",
        ("Nvidia's risk factors mention supply constraints, which management removed from its forecasts "
            "[{RISK}]."),
        "Nvidia's risk factors mention tariffs, which the USG eliminated for some products [{RISK}].",
        "The 10-K has a risk factor on China sales, which the company dropped from its guidance [{RISK}].",
    ]),
    ("independent-probe-batch-6-honest-text", "removal", [
        "Nvidia did not remove the Taiwan risk factor but kept the export risk factor [{K}].",
        "No risk factor was removed but the export risk factor was expanded [{K}].",
    ]),
    ("independent-probe-batch-6-honest-text", "none", [
        "No risk factors were removed except that one was renamed.",
        "No risk factor was removed, except in wording.",
    ]),
    ("independent-probe-batch-6-honest-text", "removal", [
        "Nvidia removed no risk factors but reworded two [{K}].",
        "Nvidia removed none of its risk factors but added one [{N}].",
        "Nvidia removed no risk factors, but it reworded the export risk factor [{K}].",
    ]),
    ("independent-probe-batch-6-honest-text", "none", [
        "Nvidia dropped none of its risk factors, but the wording of several changed.",
    ]),
    ("independent-probe-batch-6-honest-text", "removal", [
        "The Taiwan risk factor was not removed except in the sense that its heading changed [{K}].",
        "Unlike the export risk factor, the Taiwan risk factor was not dropped [{K}].",
        ("The \"No longer appears as a separate risk factor\" list does not include the Taiwan risk factor "
            "[{K}]."),
        ("The \"Passages whose wording was not found in the newer filing\" list does not contain the export "
            "sentence [{K}]."),
    ]),
    ("independent-probe-batch-6-honest-text", "none", [
        "Removed risk factors:\nNone found.",
        "Removed risk factors:\nThe list shows none found.",
        "Removed risk factors:\nNone.",
    ]),
] for text in texts]

# positive removal claims the check MUST flag (each cites an id no removed list supports, or cites nothing)
FLAG_CASES = [(family, context, t(text)) for family, context, texts in [
    ("none-tail-or-echo-ignores-an-exception-word", "removal", [
        "Removed risk factors: none besides the indebtedness risk factor [{K}].",
        "Dropped risk factors: none other than the indebtedness risk factor [{K}].",
        ("Risk factors that no longer appear as a separate risk factor: none except the indebtedness risk "
            "factor [{K}]."),
        ("Risk factors that no longer appear as a separate risk factor: none but the indebtedness risk factor "
            "[{K}]."),
        "Removed risk factors: zero, apart from the indebtedness risk factor [{K}].",
        "**No longer appears as a separate risk factor:** none, except the indebtedness risk factor [{K}]",
        ("**No longer appears as a separate risk factor:** none among the risk factors except the indebtedness "
            "risk factor [{K}]"),
        "The removed list is empty except for the indebtedness risk factor [{K}].",
        "### No longer appears as a separate risk factor\nNone except the indebtedness risk factor.",
        ("### No longer appears as a separate risk factor\nNone found.\nOne exception: the indebtedness risk "
            "factor [{K}]."),
    ]),
    ("none-tail-swallows-a-causal-or-comma-spliced-clause", "removal", [
        "Nvidia removed the Russia risk factor because its Russia exposure is now zero [{K}].",
        "Nvidia removed the Russia risk factor because its Russia exposure is now zero.",
        "The Russia risk factor was dropped because Nvidia's Russia revenue was zero [{K}].",
        "The indebtedness risk factor was removed as Nvidia's net debt is now zero [{K}].",
        "Nvidia dropped the indebtedness risk factor since its debt is zero [{K}].",
        "The indebtedness risk factor was removed now that Nvidia's debt is zero [{K}].",
        "Nvidia removed the risk factor on Russia, where its sales are now zero [{K}].",
        "Nvidia removed the risk factor on Russia because Russian sales were 0 [{K}].",
        "Nvidia removed the indebtedness risk factor [{K}], the added list is empty.",
        "The newer 10-K dropped the debt risk factor entirely, the section on debt is now empty [{K}].",
        ("The \"No longer appears as a separate risk factor\" list names the indebtedness risk [{K}], the "
            "passages list is empty."),
        ("The indebtedness risk factor is under \"No longer appears as a separate risk factor\", the added list "
            "is empty [{K}]."),
        ("The \"No longer appears as a separate risk factor\" list shows the indebtedness risk [{K}] — zero "
            "others."),
    ]),
    ("sentence-end-inside-markdown-emphasis-or-an-ellipsis", "removal", [
        "**Nvidia did not add risks.** The debt risk factor was removed [{K}].",
        "**Nvidia did not add risks.** The debt risk factor was removed.",
        "- **Not reworded.** The indebtedness risk factor was deleted [{K}].",
    ]),
    ("comma-list-or-comma-splice-does-not-end-a-negation", "removal", [
        "Nvidia did not reword, trim, or shorten the risk factor, it removed it [{K}].",
        "The indebtedness risk factor was not reworded, moved, or merged, it was removed [{K}].",
    ]),
    ("removal-presupposed-by-a-negated-communication-verb", "removal", [
        "Nvidia did not comment on the removal of the indebtedness risk factor [{K}].",
        "Nvidia did not comment on the removal of the indebtedness risk factor.",
    ]),
    ("removal-presupposed-by-a-negated-communication-verb", "unsettled", [
        "Nvidia did not comment on the removal of the export licensing risk factor [{U}].",
    ]),
    ("removal-presupposed-by-a-negated-communication-verb", "removal", [
        "Nvidia did not disclose the reason for dropping the indebtedness risk factor [{K}].",
        "Nvidia has not publicly addressed the deletion of the indebtedness risk factor [{K}].",
    ]),
    ("removal-behind-a-wh-word-or-a-causal-connective", "removal", [
        "It is not clear what prompted the removal of the indebtedness risk factor [{K}].",
        "Nvidia does not say what led to the removal of the risk factor [{K}].",
        "The newer 10-K does not mention debt due to the removal of the indebtedness risk factor [{K}].",
        "The newer 10-K does not mention debt due to the removal of the indebtedness risk factor.",
        "The 10-K does not discuss debt as a result of Nvidia removing the risk factor [{K}].",
    ]),
    ("assertive-negation-idiom", "removal", [
        "Nvidia hasn't said if it will restore the dropped risk factor [{K}].",
        "Nvidia did not think twice about removing the indebtedness risk factor [{K}].",
    ]),
    ("removal-presupposed-by-a-negated-communication-verb", "removal", [
        "Nvidia did not explicitly flag that it removed the indebtedness risk factor [{K}].",
        "Nvidia did not explicitly flag that it removed the indebtedness risk factor.",
        "Nvidia did not highlight that it had removed the indebtedness risk factor [{K}].",
        "Nvidia did not announce that it removed the indebtedness risk factor [{K}].",
        "Nvidia did not tell investors it removed the indebtedness risk factor [{K}].",
    ]),
    ("removal-in-a-relative-clause-on-a-definite-noun", "removal", [
        "Nvidia did not replace the indebtedness risk factor it removed [{K}].",
        "Nvidia did not replace the indebtedness risk factor it removed.",
        "The newer 10-K does not include the risk factor Nvidia removed [{K}].",
    ]),
    ("removal-behind-a-wh-word-or-a-causal-connective", "removal", [
        "Nvidia did not say what happened to the risk factor it removed [{K}].",
    ]),
    ("assertive-negation-idiom", "removal", [
        "It is not hard to see that Nvidia removed the indebtedness risk factor [{K}].",
        "It should not be forgotten that Nvidia removed the indebtedness risk factor [{K}].",
        "It did not take long for Nvidia to remove the indebtedness risk factor [{K}].",
        "It is not untrue that Nvidia removed the indebtedness risk factor [{K}].",
        "This is not the first time Nvidia removed a risk factor [{K}].",
    ]),
    ("removal-presupposed-by-a-negated-communication-verb", "removal", [
        "It is unclear whether investors noticed that Nvidia removed the indebtedness risk factor [{K}].",
        "It is not clear whether analysts noticed Nvidia had dropped the indebtedness risk factor [{K}].",
    ]),
    ("causal-as-followed-by-its-own-subject", "removal", [
        "Nvidia did not reword the risk factor as it removed it [{K}].",
        "Nvidia didn't reword it as the risk factor was deleted [{K}].",
    ]),
    ("removal-behind-a-wh-word-or-a-causal-connective", "removal", [
        "Nvidia does not discuss debt now that the risk factor was removed [{K}].",
        "Nvidia does not mention debt given that it removed the risk factor [{K}].",
        "You will not find it in the 10-K as Nvidia deleted the risk factor [{K}].",
    ]),
    ("causal-as-followed-by-its-own-subject", "removal", [
        ("The text check did not match the NAC sentence as its wording was not found in the newer filing "
            "[{K}]."),
    ]),
    ("causal-as-followed-by-its-own-subject", "unsettled", [
        "The export licensing risk factor [{U}] did not survive as it was removed.",
    ]),
    ("coordinator-other-than-and", "removal", [
        "Nvidia did not add new risks as well as removing the indebtedness risk factor [{K}].",
        "Nvidia did not add risks plus it removed the indebtedness risk factor [{K}].",
        "- Did not add risks & dropped the indebtedness risk factor [{K}]",
    ]),
    ("run-on-clause-after-a-negated-statement", "removal", [
        "Nvidia did not reword the risk factor it removed it [{K}].",
    ]),
    ("assertive-negation-idiom", "removal", [
        "Not fewer than three risk factors were removed [{K}].",
        "Not a few risk factors were removed [{K}].",
    ]),
    ("removal-presupposed-by-a-quantified-communication-noun", "removal", [
        "Nvidia gave no reason for removing the indebtedness risk factor [{K}].",
        "Nvidia gave no reason for removing the indebtedness risk factor.",
        "Nvidia provided no explanation for the removal of the export-control risk factor [{K}].",
        "The filing offers no explanation for dropping the Russia risk factor [{K}].",
        "No reason was given for the removal of the indebtedness risk factor [{K}].",
        "Nvidia made no announcement about removing the indebtedness risk factor [{K}].",
        "The 10-K does not explain the removal of the indebtedness risk factor [{K}].",
    ]),
    ("assertive-negation-idiom", "removal", [
        "Investors should not overlook the removal of the indebtedness risk factor [{K}].",
        "Nvidia did not wait to remove the indebtedness risk factor [{K}].",
        "Nvidia wasted no time removing the indebtedness risk factor [{K}].",
        "Nvidia did not forget to remove the indebtedness risk factor [{K}].",
        "Nvidia did not fail to remove the indebtedness risk factor [{K}].",
        "No one can deny that Nvidia removed the indebtedness risk factor [{K}].",
        "No one disputes that Nvidia removed the indebtedness risk factor [{K}].",
        "There is no denying that Nvidia removed the indebtedness risk factor [{K}].",
        "No wonder the indebtedness risk factor was removed [{K}].",
    ]),
    ("removal-presupposed-by-a-negated-communication-verb", "removal", [
        "No analyst noticed Nvidia removed the indebtedness risk factor [{K}].",
    ]),
    ("removal-in-a-relative-clause-on-a-definite-noun", "removal", [
        "Nvidia has no plans to reinstate the risk factor it removed [{K}].",
        "Nvidia has not reinstated the removed risk factor [{K}].",
        "Nvidia did not restore the dropped indebtedness risk factor [{K}].",
    ]),
    ("removal-behind-a-wh-word-or-a-causal-connective", "removal", [
        "The newer 10-K has no indebtedness risk factor anymore as Nvidia removed it [{K}].",
    ]),
    ("relative-clause-introduced-by-in-which", "removal", [
        ("Nvidia added no new risk factors to the newer 10-K in which it dropped the indebtedness risk factor "
            "[{K}]."),
    ]),
    ("removal-presupposed-by-a-negated-communication-verb", "removal", [
        ("Nvidia added no new risk factors, nor did it explain its removal of the indebtedness risk factor "
            "[{K}]."),
    ]),
    ("sentence-end-inside-markdown-emphasis-or-an-ellipsis", "removal", [
        "**No new risk factors were added.** The indebtedness risk factor was removed [{K}].",
        "**Nvidia added no new risk factors.** The indebtedness risk factor was removed.",
        "*Nvidia added no new risks.* The debt risk factor was removed [{K}].",
        "**No risk factors were added.** Nvidia removed the indebtedness risk factor [{K}].",
        "- **No change to the export risk.** The indebtedness risk factor was removed [{K}].",
        "**Short answer: no new risks.** The indebtedness risk factor was dropped [{K}].",
        "Nvidia added nothing… The debt risk factor was removed [{K}].",
    ]),
    ("removal-clause-with-no-disclosure-noun-of-its-own", "removal", [
        "The older 10-K had a risk factor on indebtedness. Nvidia has since removed it [{K}].",
        "The older filing discussed indebtedness. The company later dropped it [{K}].",
        "The older 10-K had an indebtedness risk factor and a Russia risk factor; Nvidia removed both [{K}].",
        "Neither the indebtedness risk factor nor the Russia risk factor survived; both were removed [{K}].",
        "Nvidia's older 10-K had an indebtedness risk factor, which Nvidia removed [{K}].",
        "Nvidia's older 10-K had an indebtedness risk factor, which the company later dropped [{K}].",
        "Nvidia did not keep the debt risk factor, which it removed [{K}].",
        "Nvidia did not keep the indebtedness risk factor, which the check lists as removed [{K}].",
        "The newer 10-K has no indebtedness risk factor; Nvidia removed it [{K}].",
        "No, Nvidia removed it [{K}].",
        "No — Nvidia dropped it [{K}].",
        "Yes, it was removed [{K}].",
        "Yes. Nvidia removed it [{K}].",
    ]),
    ("terse-bullet-or-table-predicate", "removal", [
        "| Risk factor | Change |\n|---|---|\n| Indebtedness | Removed [{K}] |",
        "Risk factor changes:\n- Indebtedness: removed [{K}]\n- Export controls: reworded [{NEWREW}]",
        "### Risk factor changes\n- **Indebtedness** — dropped [{K}]",
        "- Debt: no longer included [{K}]",
    ]),
    ("negation-inside-a-relative-clause", "removal", [
        "The risk factor Nvidia never updated was deleted [{K}].",
        "The risk factor Nvidia did not reword no longer appears in the newer 10-K [{K}].",
        "The only risk factor not reworded was removed [{K}].",
        "A risk factor that was not reworded was removed [{K}].",
    ]),
    ("none-word-inside-a-compound-or-abbreviation", "removal", [
        "The China zero-COVID risk factor was removed [{K}].",
        "Risk factor No. 12 (indebtedness) was removed [{K}].",
        "The no-China-sales risk factor was removed [{K}].",
    ]),
    ("assertive-negation-idiom", "removal", [
        "No small number of risk factors were removed [{K}].",
    ]),
    ("none-tail-swallows-a-causal-or-comma-spliced-clause", "removal", [
        "Nvidia removed the indebtedness risk factor because its debt is now zero [{K}].",
        "Nvidia removed the indebtedness risk factor because its debt is now zero.",
        "Nvidia dropped the Russia risk factor as its Russia sales are 0 [{K}].",
        "The newer 10-K dropped the Russia risk factor since sales there were zero [{K}].",
        "Nvidia removed the indebtedness risk factor because its debt balance was nothing material [{K}].",
    ]),
    ("numeric-zero-read-as-a-none-quantifier", "removal", [
        "Because its Russia revenue fell to 0 the Russia risk factor was dropped [{K}].",
        "After Russia revenue reached 0 in fiscal 2023 the Russia risk factor was removed [{K}].",
        "Since Russia revenue fell to 0 the Russia risk factor was dropped.",
    ]),
    ("removal-presupposed-by-a-quantified-communication-noun", "removal", [
        ("Nvidia provides no explanation in the fiscal year ended January 25, 2026 annual report on Form 10-K "
            "for its decision to remove the export risk factor [{K}]."),
        ("Investors got no warning from Nvidia's management over the course of the whole year that it would "
            "drop the export risk factor [{K}]."),
        ("Nvidia made no comment in its newer annual report on Form 10-K for fiscal 2026 about its decision to "
            "drop the export risk factor."),
    ]),
    ("comma-list-or-comma-splice-does-not-end-a-negation", "removal", [
        "Nvidia did not add risks, it reworded two, it removed the indebtedness risk factor [{K}].",
        "Nvidia did not add new risks — it reworded two — it removed the indebtedness risk factor [{K}].",
        "Nvidia did not add risks, or reword any, it removed the indebtedness risk factor [{K}].",
        "Nvidia did not add risks, it reworded two, it removed the indebtedness risk factor.",
        "Nvidia did not add risks, it reworded two, it removed these:\n- Indebtedness risk [{K}]",
    ]),
    ("none-tail-or-echo-ignores-an-exception-word", "removal", [
        ("**No longer appears as a separate risk factor:** none found except the indebtedness risk factor "
            "[{K}]."),
        "**No longer appears as a separate risk factor:** none except the indebtedness risk factor.",
        "No longer appears as a separate risk factor: none other than the indebtedness risk factor.",
    ]),
    ("label-echo-followed-by-an-arbitrary-parenthetical", "removal", [
        "**No longer appears as a separate risk factor** (the indebtedness risk factor)",
        "### No longer appears as a separate risk factor (the indebtedness risk factor was omitted)",
        "### No longer appears as a separate risk factor (1 of 1: indebtedness risk factor)",
        "| No longer appears as a separate risk factor | (indebtedness risk) |",
        "**Passages whose wording was not found in the newer filing** (the indebtedness sentence)",
    ]),
    ("label-echo-followed-by-an-arbitrary-parenthetical", "unsettled", [
        "### Not matched (the export licensing risk factor was removed)",
    ]),
    ("none-in-the-label-or-first-item-hides-later-items", "removal", [
        "### No longer appears as a separate risk factor (none found)\n- Indebtedness risk [{K}]",
        "### No longer appears as a separate risk factor: none\n- Indebtedness risk [{K}]",
        "### No longer appears as a separate risk factor\n- None found.\n- Export risk",
    ]),
    ("none-tail-or-echo-ignores-an-exception-word", "removal", [
        "### No longer appears as a separate risk factor\nNone, except the indebtedness risk factor.",
    ]),
    ("list-mention-whose-id-is-in-the-previous-clause", "removal", [
        "The indebtedness risk [{K}], which is on the \"No longer appears as a separate risk factor\" list.",
        "The indebtedness risk [{K}]; it is on the \"No longer appears as a separate risk factor\" list.",
    ]),
    ("none-tail-or-echo-ignores-an-exception-word", "removal", [
        ("The \"No longer appears as a separate risk factor\" list is empty except for the indebtedness risk "
            "factor."),
    ]),
    ("removal-presupposed-by-a-negated-communication-verb", "removal", [
        ("The check could not determine whether customers noticed that Nvidia removed the indebtedness risk "
            "factor [{K}]."),
    ]),
    ("removal-behind-a-wh-word-or-a-causal-connective", "removal", [
        ("The check cannot tell whether the older wording is better now that Nvidia removed the export risk "
            "factor [{K}]."),
    ]),
    ("removal-in-a-relative-clause-on-a-definite-noun", "removal", [
        "The check cannot tell whether the export risk factor that Nvidia removed was replaced [{K}].",
    ]),
    ("removal-presupposed-by-a-negated-communication-verb", "removal", [
        "It is unclear whether investors noticed that Nvidia dropped the indebtedness risk factor [{K}].",
    ]),
    ("sibling-bullet-under-a-bullet-label", "removal", [
        "- **No longer appears as a separate risk factor:**\n- Indebtedness risk [{K}]",
    ]),
    ("removal-presupposed-by-a-quantified-communication-noun", "removal", [
        ("Nvidia made no comment in its fiscal 2026 annual report on Form 10-K about the indebtedness risk "
            "factor that it removed [{K}]."),
    ]),
    ("removal-in-a-relative-clause-on-a-definite-noun", "removal", [
        ("The text check found no wording changes in the fiscal year ended January 25, 2026 annual report for "
            "the export licensing risk factor that was removed [{K}]."),
    ]),
    ("and-followed-by-an-assertive-not", "removal", [
        "Nvidia did not add new risk factors and not surprisingly removed the indebtedness risk factor [{K}].",
    ]),
    ("independent-probe-batch-1-positive-claims", "removal", [
        "Nvidia did not add any new risk factors and removed the indebtedness risk factor [{K}].",
        "Nvidia added no risks; it removed the indebtedness risk factor [{K}].",
        "Although the wording did not change, Nvidia removed the indebtedness risk factor [{K}].",
        "The 10-K shows no new risk factors. However, the Russia risk factor was dropped [{K}].",
        "Nvidia did not just reword the export risk factor — it removed it [{K}].",
        "The export risk factor was not reworded; it was removed [{K}].",
        "It is not that Nvidia reworded the risk factor, it removed it [{K}].",
        "Nvidia did not keep the debt risk factor: it removed it [{K}].",
        "Nvidia no longer discloses the risk factor on Russia [{K}].",
        "The indebtedness risk factor no longer appears in the newer 10-K [{K}].",
        "Nvidia stopped disclosing the export risk factor [{K}].",
        "The risk factor on Hong Kong was eliminated from the newer 10-K [{K}].",
        "Nvidia deleted the Taiwan risk factor between the two filings [{K}].",
        "Nvidia discontinued its disclosure on supply constraints [{K}].",
        ("Between the two 10-Ks, Nvidia dropped three risk factors, including the indebtedness risk factor "
            "[{K}]."),
        "No one expected it, but Nvidia removed the indebtedness risk factor [{K}].",
        "Not only did Nvidia reword the export risk, it also dropped the Russia risk factor [{K}].",
        "Not surprisingly, the risk factor on tariffs was dropped [{K}].",
        "There were no new risk factors, and the Russia risk factor was removed [{K}].",
        "Nvidia added nothing new and removed the Russia risk factor [{K}].",
        "None of the new risk factors are related to Russia, but the Russia risk factor was removed [{K}].",
        "The risk factor is not mentioned in the newer 10-K because it was removed [{K}].",
        "Nvidia did not explain why, yet the risk factor was removed [{K}].",
        "Nvidia never said it would drop the risk factor, but it did drop the indebtedness risk factor [{K}].",
        "The company neither reworded nor kept the risk factor; it deleted it [{K}].",
        "No risk factors were added, and one risk factor was removed [{K}].",
        "None of the risk factors were reworded but the Russia risk factor was removed [{K}].",
        "Nvidia removed no fewer than two risk factors [{K}].",
        "At least two risk factors were removed [{K}].",
        "Zero risk factors were reworded, and the Russia risk factor was dropped [{K}].",
        "The wording of the export risk factor was not found in the newer filing [{K}].",
        "The text check found no matching text in the newer filing for the Russia risk factor [{K}].",
        "The Russia risk factor no longer appears as a separate risk factor [{K}].",
        "This sentence was not found in the newer filing [{K}].",
        "Some passages had wording not found in the newer filing [{K}].",
        "The newer 10-K no longer includes the risk factor about Russia [{K}].",
        "Nvidia did not reword anything: the Russia risk factor was deleted [{K}].",
        "The comparison does not show that Nvidia reworded the risk factor; it removed it [{K}].",
        ("The comparison did not find new risk factors, but it did find that Nvidia removed the Russia risk "
            "factor [{K}]."),
        "The text check does not show any rewording, however the Russia risk factor was removed [{K}].",
        ("The check does not indicate that the export risk factor changed, because the Russia risk factor was "
            "removed [{K}]."),
        "The comparison did not show what changed, and Nvidia removed the export risk factor [{K}].",
        ("The comparison does not show that the export risk factor was reworded and shows the Russia risk "
            "factor was removed [{K}]."),
        ("The text check does not show that the export risk factor was reworded, so Nvidia removed the Russia "
            "risk factor [{K}]."),
        "Nvidia did not explain, but the Russia risk factor was dropped [{K}].",
        "It is not clear, but Nvidia deleted the risk factor on Russia [{K}].",
        "The comparison did not show any new risk factors — Nvidia dropped the Russia risk factor [{K}].",
        "Nvidia did not reword the risk factor, it eliminated it [{K}].",
        "Nvidia did not add risks, Nvidia removed the Russia risk factor [{K}].",
        "Nvidia, AMD or Micron did not add risks; Nvidia removed the Russia risk factor [{K}].",
        "None of Nvidia, AMD or Micron added risks, but Nvidia dropped the Russia risk factor [{K}].",
        "Neither AMD nor Micron changed, yet Nvidia removed the Russia risk factor [{K}].",
        ("The text check found no risk factors that Nvidia added, and the Russia risk factor was removed "
            "[{K}]."),
        "Nvidia made no comment, and the Russia risk factor was dropped [{K}].",
        "There is no explanation, but the Russia risk factor was removed [{K}].",
        "Nvidia provided no explanation. The Russia risk factor was removed [{K}].",
        "Nvidia offered no reasons; it removed the export risk factor [{K}].",
        "Nvidia didn't add any risk factors, it deleted the Russia risk factor [{K}].",
        "Nvidia did not, however, keep the Russia risk factor; it removed it [{K}].",
        "Nvidia did not, as far as we can tell, add risks. It removed the Russia risk factor [{K}].",
        "The check did not say whether it was reworded, but the Russia risk factor was removed [{K}].",
        "Whether or not it was reworded, the Russia risk factor was removed [{K}].",
        "Whether the export risk factor was reworded or not, the Russia risk factor was dropped [{K}].",
        ("The check cannot tell whether the export risk factor was reworded, and the Russia risk factor was "
            "removed [{K}]."),
        "It is unclear whether it was reworded; the Russia risk factor was removed [{K}].",
        ("It is unclear whether Nvidia reworded the export risk factor. Nvidia removed the Russia risk factor "
            "[{K}]."),
        "If the export risk factor was reworded, the Russia risk factor was still removed [{K}].",
        "Any risk factor could have been reworded, but the Russia risk factor was removed [{K}].",
        ("It would be wrong to say the export risk factor was dropped, but the Russia risk factor was removed "
            "[{K}]."),
        "It is incorrect that the export risk factor changed; the Russia risk factor was removed [{K}].",
        "That is different from rewording: Nvidia removed the Russia risk factor [{K}].",
        "Unlike the export risk factor, Nvidia dropped the Russia risk factor [{K}].",
        "Unlike rewording, removal is clear: Nvidia removed the Russia risk factor [{K}].",
        "Rather than rewording the risk factor, Nvidia removed the Russia risk factor [{K}].",
        "Instead of rewording it, Nvidia deleted the Russia risk factor [{K}].",
        "Without rewording it, Nvidia removed the Russia risk factor [{K}].",
        "Without notice, Nvidia dropped the Russia risk factor [{K}].",
        "The Russia risk factor, which Nvidia did not reword, was removed [{K}].",
        "The Russia risk factor, which was not reworded, no longer appears in the newer 10-K [{K}].",
        ("The export risk factor, not the Russia one, was reworded, and Nvidia dropped the Taiwan risk factor "
            "[{K}]."),
        "Nvidia dropped the Taiwan risk factor, not the export risk factor [{K}].",
        "The Taiwan risk factor was removed, not reworded [{K}].",
        "The Taiwan risk factor was removed rather than reworded [{K}].",
        "The Taiwan risk factor was removed, but not the export risk factor [{K}].",
        "The Taiwan risk factor was removed and not reworded [{K}].",
        "The Taiwan risk factor was not just reworded but removed [{K}].",
        "The Taiwan risk factor was not reworded but removed [{K}].",
        "The Taiwan risk factor was never reworded, and it was removed [{K}].",
        "Nvidia never mentioned Taiwan again; it dropped the Taiwan risk factor [{K}].",
        "The check did not show growth, but Nvidia removed the Taiwan risk factor [{K}].",
        "Nvidia dropped no risk factors related to China but removed the Taiwan risk factor [{K}].",
        "Nvidia removed no risk factors on China but dropped the Taiwan risk factor [{K}].",
        "Nvidia did not remove the China risk factor, but it deleted the Taiwan risk factor [{K}].",
        "Nvidia did not remove the China risk factor. It deleted the Taiwan risk factor [{K}].",
        "Nvidia did not remove the China risk factor and it deleted the Taiwan risk factor [{K}].",
        "Nvidia did not remove the China risk factor as it deleted the Taiwan risk factor [{K}].",
        "The list of removed risk factors names the Taiwan risk factor [{K}].",
        ("The Taiwan risk factor is one of the risk factors that no longer appear as a separate risk factor "
            "[{K}]."),
        "Removed risk factors: the Taiwan risk factor [{K}].",
        "Removed risk factors (1): Taiwan [{K}]",
        "**Removed:** the Taiwan risk factor [{K}]",
        "- Removed: Taiwan risk factor [{K}]",
        "### Removed risk factors\n- Taiwan risk factor [{K}]",
        "### Removed risk factors\nThe Taiwan risk factor [{K}].",
        "### Risk factors that no longer appear\n- Taiwan [{K}]",
        "### No longer appears as a separate risk factor\nTaiwan risk factor [{K}].",
        ("### No longer appears as a separate risk factor\nNone found for China. The Taiwan risk factor [{K}] "
            "is listed."),
        "### No longer appears as a separate risk factor\nThe list shows none found.\nTaiwan risk factor [{K}].",
        "### No longer appears as a separate risk factor\n- None found.\n- Taiwan risk factor [{K}]",
        "**No longer appears as a separate risk factor:**\n- Taiwan risk factor [{K}]",
        "**No longer appears as a separate risk factor:** the Taiwan risk factor [{K}]",
        ("**No longer appears as a separate risk factor:** none found for China, but the Taiwan risk factor "
            "[{K}]."),
        "**No longer appears as a separate risk factor:** none, apart from the Taiwan risk factor [{K}].",
        "**No longer appears as a separate risk factor:** empty other than the Taiwan risk factor [{K}].",
        ("The Taiwan risk factor is on the list of risk factors that no longer appear as a separate risk "
            "factor [{K}]."),
        "The \"No longer appears as a separate risk factor\" list contains the Taiwan risk factor [{K}].",
        "The Taiwan risk factor appears under \"No longer appears as a separate risk factor\" [{K}].",
        "Only the Taiwan risk factor is on the \"No longer appears as a separate risk factor\" list [{K}].",
        "The \"No longer appears as a separate risk factor\" list has one entry: the Taiwan risk factor [{K}].",
        ("The \"No longer appears as a separate risk factor\" list is not empty: it holds the Taiwan risk factor "
            "[{K}]."),
        "The \"No longer appears as a separate risk factor\" list shows the Taiwan risk factor, not none [{K}].",
        ("The \"No longer appears as a separate risk factor\" list is empty, except that the Taiwan risk factor "
            "[{K}] is on it."),
        ("The Passages \"whose wording was not found in the newer filing\" list contains the export sentence "
            "[{K}]."),
        ("The Taiwan risk factor was found in the older filing but no longer appears in the newer filing "
            "[{K}]."),
        "The Taiwan risk factor was in the older filing, and it is not found in the newer filing [{K}].",
        "Nvidia dropped the Taiwan risk factor, which means the text check found no matching text [{K}].",
        ("Nvidia removed the Taiwan risk factor, and the text check found no matching text for it in the newer "
            "filing [{K}]."),
        ("The Taiwan risk factor was removed, the text check found no matching text for it in the newer filing "
            "[{K}]."),
        "The Taiwan risk factor no longer appears, which means the text check found no matching text [{K}].",
    ]),
    ("independent-probe-batch-2-positive-claims", "removal", [
        "Nvidia did not explain the removal of the Russia risk factor [{K}].",
        ("The comparison does not indicate that any risk factor was reworded, but shows that the Russia risk "
            "factor was removed [{K}]."),
        ("The comparison does not show a new risk factor; it shows that Nvidia removed the Russia risk factor "
            "[{K}]."),
        ("The comparison does not show that Nvidia added risks and shows that Nvidia dropped the Russia risk "
            "factor [{K}]."),
        ("The comparison does not show that the export risk factor was reworded, only that the Russia risk "
            "factor was removed [{K}]."),
        ("The check does not show that anything changed except that Nvidia removed the Russia risk factor "
            "[{K}]."),
        ("The check does not show that the export risk factor was reworded. Nvidia removed the Russia risk "
            "factor [{K}]."),
        "Nvidia did not add risks as it removed the Russia risk factor [{K}].",
        "Nvidia asked whether the export risk factor was needed, and then removed it [{K}].",
        "Nvidia decided whether to keep the export risk factor and removed it [{K}].",
        "Whether it was reworded or not is unclear, but Nvidia removed the export risk factor [{K}].",
        "The Russia risk factor is among the risk factors that were removed [{K}].",
        "The Russia risk factor is not among the reworded ones, it is among the removed [{K}].",
        "Not all risk factors survived: the Russia risk factor was removed [{K}].",
        "### Removed risk factors\nNo explanation given. The Taiwan risk factor [{K}].",
        "### Removed risk factors\nNone found, but see the Taiwan risk factor [{K}].",
        "### Removed risk factors\nNo removals were found except the Taiwan risk factor.",
        "Nvidia did not add risks, reword any, or trim, it removed the Russia risk factor [{K}].",
        "Nvidia did not add, reword or trim risks, Nvidia removed the Russia risk factor [{K}].",
        "Nvidia did not add risks for Micron Technology, Inc. Nvidia removed the Russia risk factor [{K}].",
        "Nvidia did not add risks in Jan. 2026 and removed the Russia risk factor [{K}].",
        "Nvidia did not add tariffs, sanctions, etc. Nvidia removed the Russia risk factor [{K}].",
        "| Taiwan | Removed [{K}] |",
        "It was removed [{K}].",
        "It would be incorrect to say that Nvidia reworded the risk factor; it removed it [{K}].",
        "It is wrong that Nvidia kept the export risk factor: it removed it [{K}].",
        "Wrong: Nvidia removed the Russia risk factor [{K}].",
        "That statement is false; Nvidia removed the Russia risk factor [{K}].",
        "The claim is false, because Nvidia removed the Russia risk factor [{K}].",
        "It is false to say Nvidia kept the risk factor, since it removed it [{K}].",
        "Unknown to investors, Nvidia removed the Russia risk factor [{K}].",
        "It is unverified, but Nvidia removed the Russia risk factor [{K}].",
        "Some passages had wording that was not found in the newer filing [{K}].",
        "The claim that Nvidia removed the Russia risk factor is correct [{K}].",
        "The claim that Nvidia removed the Russia risk factor is supported by the comparison [{K}].",
        "The suggestion that the Russia risk factor was removed is true [{K}].",
        ("Nvidia did not say that the export risk factor changed, though the Russia risk factor was removed "
            "[{K}]."),
        "The Russia risk factor was not reworded in the newer 10-K, unlike others, and was removed [{K}].",
        "The Russia risk factor was not reworded and Nvidia removed it entirely [{K}].",
        "The Russia risk factor was not reworded, and Nvidia removed it entirely [{K}].",
        "No new risk factors were listed; Nvidia removed the Russia risk factor [{K}].",
        "**No new risk factors.** Nvidia removed the Russia risk factor [{K}].",
        "> No new risk factors were added. Nvidia removed the Russia risk factor [{K}].",
        "- No new risk factors were added.\n- Nvidia removed the Russia risk factor [{K}].",
        ("Nvidia added no new risk factors this year that the check found, but it removed the Russia risk "
            "factor [{K}]."),
        "The check found no new risk factors that Nvidia added; Nvidia removed the Russia risk factor [{K}].",
        ("The text comparison did not identify any risk factor as reworded, and identified the Russia risk "
            "factor as removed [{K}]."),
        ("The text comparison did not identify any risk factor as reworded but identified the Russia risk "
            "factor as removed [{K}]."),
        "The comparison did not show any risk factor as reworded or the Russia risk factor as removed [{K}].",
        ("The comparison did not identify any wording change and marked the Russia risk factor as removed "
            "[{K}]."),
        "The comparison does not mention new risk factors and lists the Russia risk factor as removed [{K}].",
        "The comparison does not mention new risk factors; it lists the Russia risk factor as removed [{K}].",
        "The comparison does not mention new risk factors — it lists the Russia risk factor as removed [{K}].",
        "Nvidia did not disclose any new risks; the Russia risk factor was dropped [{K}].",
        "Nvidia did not disclose any new risks; instead, the Russia risk factor was dropped [{K}].",
        "Nvidia did not disclose any new risks; rather, it dropped the Russia risk factor [{K}].",
        "There was no rewording; there was a removal of the Russia risk factor [{K}].",
        "No wording change explains it: the Russia risk factor was removed [{K}].",
        "No wording change explains it; the Russia risk factor was removed [{K}].",
        "No wording change was found, and the Russia risk factor was removed [{K}].",
        "No wording change was found. The Russia risk factor was removed [{K}].",
        "Nothing was reworded and the Russia risk factor was removed [{K}].",
        "Nothing was reworded, the Russia risk factor was removed [{K}].",
        "Nothing was reworded; the Russia risk factor was removed [{K}].",
        "Nothing was reworded. The Russia risk factor was removed [{K}].",
        "Nothing changed apart from the removal of the Russia risk factor [{K}].",
        "Nothing changed besides the removal of the Russia risk factor [{K}].",
        "Nothing changed but the removal of the Russia risk factor [{K}].",
        "Nothing else changed than the removal of the Russia risk factor [{K}].",
        "No change occurred other than the removal of the Russia risk factor [{K}].",
        "There was no change except for the removal of the Russia risk factor [{K}].",
        "No risk factor except the Russia risk factor was removed [{K}].",
        "No risk factor other than the Russia risk factor was removed [{K}].",
        "No risk factor but the Russia risk factor was removed [{K}].",
        "No risk factor besides the Russia risk factor was removed [{K}].",
        "None of the risk factors was removed except the Russia one [{K}].",
        "Apart from the Russia risk factor, none was dropped [{K}].",
        "Except for the Russia risk factor, nothing was removed [{K}].",
        "Other than the Russia risk factor, no risk factors were dropped [{K}].",
        "Aside from the Taiwan risk factor, nothing was deleted [{K}].",
        "Besides the Taiwan disclosure, no statement was removed [{K}].",
        "The only removal was the Russia risk factor [{K}].",
        "The one removal was the Taiwan risk factor [{K}].",
        "Only the Taiwan risk factor was removed [{K}].",
        "Just the Taiwan risk factor was dropped [{K}].",
        "The Taiwan risk factor alone was removed [{K}].",
    ]),
    ("independent-probe-batch-3-positive-claims", "removal", [
        "Nvidia removed nothing but the Taiwan risk factor [{K}].",
        "Nothing was removed but the Taiwan risk factor [{K}].",
        "Nvidia removed none but the Taiwan risk factor [{K}].",
        "Nvidia removed no one risk factor but the Taiwan one [{K}].",
        "Nvidia dropped none of its risk factors except the Taiwan risk factor [{K}].",
        "Nvidia removed no risk factors other than the Taiwan risk factor [{K}].",
        "Nvidia removed no risk factors apart from the Taiwan risk factor [{K}].",
        "Nvidia removed no risk factors besides the Taiwan risk factor [{K}].",
        "Nvidia deleted nothing except the Taiwan risk factor [{K}].",
        "Nvidia removed no fewer than two risk factors, including the Taiwan risk factor [{K}].",
        "Nvidia dropped no risk factors in 2025 but dropped the Taiwan risk factor in 2026 [{K}].",
        "Nvidia dropped no risk factors, but the Taiwan risk factor was removed [{K}].",
        "Nvidia removed none of its risk factors, yet the Taiwan risk factor no longer appears [{K}].",
        "The comparison identified no removals; nevertheless, the Taiwan risk factor was dropped [{K}].",
        "The comparison found nothing removed, though the Taiwan risk factor was in fact deleted [{K}].",
        "The Taiwan risk factor was reworded and the export risk factor was removed [{K}].",
        "The Taiwan risk factor was reworded; the export risk factor was removed [{K}].",
        "The Taiwan risk factor was reworded. The export risk factor was removed [{K}].",
        "The Taiwan risk factor was reworded, while the export risk factor was removed [{K}].",
        "The Taiwan risk factor was reworded whereas the export risk factor was removed [{K}].",
        "The Taiwan risk factor was reworded, but the export risk factor was removed [{K}].",
        "The Taiwan risk factor was reworded, and the export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed, and the export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed; the export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed. The export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed but the export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed while the export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed, whereas the export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed (the export risk factor was dropped) [{K}].",
        "The Taiwan risk factor was not removed - the export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed — the export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed: the export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed, the export risk factor was dropped [{K}].",
        "The Taiwan risk factor was not removed, it was the export risk factor that was dropped [{K}].",
        "It was not the Taiwan risk factor that was reworded; the export risk factor was removed [{K}].",
        "No wording changed in the Taiwan risk factor; Nvidia dropped the export risk factor [{K}].",
        "There was no change to Taiwan, but Nvidia removed the export risk factor [{K}].",
        "There was no change to Taiwan. Nvidia removed the export risk factor [{K}].",
        "Whether the Taiwan risk factor changed is unclear. Nvidia removed the export risk factor [{K}].",
        "Whether the Taiwan risk factor changed is unclear; Nvidia removed the export risk factor [{K}].",
        "Whether the Taiwan risk factor changed is unclear, but Nvidia removed the export risk factor [{K}].",
        "It is unclear whether the Taiwan risk factor changed; Nvidia removed the export risk factor [{K}].",
        "It is not clear whether the Taiwan risk factor changed. The export risk factor was removed [{K}].",
        ("The check cannot tell whether the Taiwan risk factor changed, but the export risk factor was removed "
            "[{K}]."),
        "The check could not verify the Taiwan risk factor and the export risk factor was removed [{K}].",
        "The check could not verify the Taiwan risk factor; the export risk factor was removed [{K}].",
        "The check did not verify the Taiwan risk factor, but the export risk factor was removed [{K}].",
        ("It would be wrong to say that the Taiwan risk factor was removed, but the export risk factor was "
            "dropped [{K}]."),
        ("It would be wrong to say that the Taiwan risk factor changed; the export risk factor was dropped "
            "[{K}]."),
        "It is incorrect that the Taiwan risk factor changed. The export risk factor was removed [{K}].",
        "It is false to claim the Taiwan risk factor changed: the export risk factor was removed [{K}].",
        ("The claim that the Taiwan risk factor changed is false, but the export risk factor was removed "
            "[{K}]."),
        ("The claim that the Taiwan risk factor changed is not supported; the export risk factor was removed "
            "[{K}]."),
        ("The claim that the Taiwan risk factor was reworded is supported, and the export risk factor was "
            "removed [{K}]."),
        "The Taiwan risk factor is not among the reworded ones; it was removed [{K}].",
        "The Taiwan risk factor is not among the reworded risk factors, so it was removed [{K}].",
        "The Taiwan risk factor is not one of the surviving ones, so it was dropped [{K}].",
        "The Taiwan risk factor is not one of the reworded ones, it was removed [{K}].",
        "The Taiwan risk factor is one of those removed [{K}].",
        "The Taiwan risk factor is among the removed [{K}].",
        "The Taiwan risk factor was among the removed [{K}].",
        "The Taiwan risk factor was one of the removed risk factors [{K}].",
        "The Taiwan risk factor is one of the removed [{K}].",
        "Among the removed risk factors is the Taiwan risk factor [{K}].",
        "The removed risk factors include the Taiwan risk factor [{K}].",
        "The removed risk factors are the Taiwan risk factor and the export risk factor [{K}].",
        "The risk factors removed were the Taiwan risk factor and the export risk factor [{K}].",
        "The risk factors that were removed include the Taiwan risk factor [{K}].",
        "The risk factor that Nvidia removed is the Taiwan risk factor [{K}].",
        "The risk factor Nvidia removed was the Taiwan risk factor [{K}].",
        "The risk factor that was removed was the Taiwan risk factor [{K}].",
        "The one that was removed was the Taiwan risk factor [{K}].",
        "What was removed was the Taiwan risk factor [{K}].",
        "What Nvidia removed was the Taiwan risk factor [{K}].",
        "The only risk factor that was removed is the Taiwan risk factor [{K}].",
        "A single risk factor was removed: the Taiwan risk factor [{K}].",
        "One risk factor was removed, namely the Taiwan risk factor [{K}].",
        "Two risk factors were removed: Taiwan and China [{K}].",
        "Three risk factors were dropped, according to the text check [{K}].",
        "According to the text check, the Taiwan risk factor was removed [{K}].",
        "According to the comparison, Nvidia did not add risks, but the Taiwan risk factor was removed [{K}].",
        "Based on the text check, the Taiwan risk factor was dropped [{K}].",
        "Based on the text check, Nvidia removed the Taiwan risk factor [{K}].",
        "The text check indicates that the Taiwan risk factor was removed [{K}].",
        "The text check shows that the Taiwan risk factor was removed [{K}].",
        "The text check confirms that the Taiwan risk factor was removed [{K}].",
        "The text check found that the Taiwan risk factor was removed [{K}].",
        "The text check found the Taiwan risk factor removed [{K}].",
        ("The text check found that no risk factor was reworded and that the Taiwan risk factor was removed "
            "[{K}]."),
        ("The text check did not find that the Taiwan risk factor was reworded and found that it was removed "
            "[{K}]."),
        "The text check did not find rewording, and it found that the Taiwan risk factor was removed [{K}].",
        "The text check did not find rewording; it found that the Taiwan risk factor was removed [{K}].",
        "The text check did not find rewording. It found that the Taiwan risk factor was removed [{K}].",
        "The text check did not find rewording but found that the Taiwan risk factor was removed [{K}].",
        "The text check found no rewording but found that the Taiwan risk factor was removed [{K}].",
        "The text check found no new risk factors and found the Taiwan risk factor removed [{K}].",
        "The text check found no new risk factors and did find that the Taiwan risk factor was removed [{K}].",
        ("The text check found no new risk factors and also noted that the Taiwan risk factor was dropped "
            "[{K}]."),
        "The text check found no new risk factors and additionally the Taiwan risk factor was dropped [{K}].",
        "The text check found no new risk factors; additionally, the Taiwan risk factor was dropped [{K}].",
        "The text check found no new risk factors. Additionally, the Taiwan risk factor was dropped [{K}].",
        "**Findings**\n- No new risk factors.\n- The Taiwan risk factor was dropped [{K}].",
        "**Findings**\n- No new risk factors\n- Removed: Taiwan risk factor [{K}]",
        "### Findings\nNo new risk factors were found. The Taiwan risk factor was removed [{K}].",
        "### Findings\nNo new risk factors were found.\nThe Taiwan risk factor was removed [{K}].",
        "No new risk factors were found.\nThe Taiwan risk factor was removed [{K}].",
        "No new risk factors were found.\n\nThe Taiwan risk factor was removed [{K}].",
        "Short answer: yes. The Taiwan risk factor was removed [{K}].",
        "Short answer: No new risk factors, but the Taiwan risk factor was removed [{K}].",
        "Yes: the Taiwan risk factor was removed [{K}].",
        "Yes - the Taiwan risk factor was removed [{K}].",
        "Yes, Nvidia removed the Taiwan risk factor [{K}].",
        "Yes, Nvidia dropped it [{K}].",
        "Yes, they were dropped [{K}].",
        "Yes, both were removed [{K}].",
        "Yes; it was removed [{K}].",
        "No, it was not reworded; it was removed [{K}].",
        "No, not reworded: Nvidia removed it [{K}].",
        "No - the risk factor was not reworded, Nvidia removed it [{K}].",
        "It was removed [{K}], not reworded.",
        "It was removed, not reworded [{K}].",
        "It was dropped [{K}], not merged.",
        "Removed, not reworded [{K}].",
        "Removed [{K}].",
        "Removed: Taiwan risk factor [{K}].",
        "Taiwan: removed [{K}].",
        "Taiwan risk factor - removed [{K}].",
        "Taiwan risk factor — removed [{K}].",
        "| Taiwan risk factor | Removed | [{K}] |",
        "| Risk factor | Status |\n|---|---|\n| Taiwan | Removed [{K}] |\n| Export | Reworded [{NEWREW}] |",
        "| Risk factor | Status |\n|---|---|\n| Taiwan | no longer appears [{K}] |",
        "| Risk factor | Status |\n|---|---|\n| Taiwan | dropped [{K}] |",
    ]),
    ("independent-probe-batch-4-positive-claims", "removal", [
        "| Risk factor | Status |\n|---|---|\n| Taiwan | Removed [{K}] |",
        "| Risk factor | Status |\n|---|---|\n| Taiwan | Removed [{K}] |\n| Export | Reworded [{NEWREW}] |",
        "| Risk factor | Status |\n|---|---|\n| Export | Reworded [{K}] |\n| Taiwan | Removed [{K}] |",
        "- Taiwan: reworded [{K}]\n- Export: no longer appears as a separate risk factor [{K}]",
        "- **Export** — no longer appears as a separate risk factor [{K}]\n- **Taiwan** — reworded [{K}]",
        "**Removed:**\n- Taiwan [{K}]\n**Reworded:**\n- Export [{NEWREW}]",
        ("**Removed risk factors:**\n- None found.\n- Taiwan [{K}]\n\n**Reworded risk factors:**\n- Export "
            "[{NEWREW}]"),
        "- Removed risk factors:\n  - Taiwan [{K}]",
        "- Removed risk factors:\n  - Taiwan [{R}]\n  - Export [{K}]",
        "Removed risk factors (2):\n1. Taiwan [{R}]\n2. Export [{K}]",
        "**Nvidia removed the Taiwan risk factor [{K}]**",
        "**Nvidia removed the Taiwan risk factor**\nThe Export risk factor was reworded [{K}].",
        "### Removed risk factors\n- Taiwan risk factor [{R}]\n- Export risk factor [{K}]",
        "### Removed\n- Taiwan [{R}]\n- Export [{K}]",
        "### Removed\nTaiwan [{R}]\nExport [{K}]",
        ("### Removed risk factors\n- Taiwan risk factor [{R}]\n\n### Reworded risk factors\n- Export risk factor "
            "[{K}]\nNvidia also dropped the China risk factor [{K}]."),
        ("The Taiwan risk factor no longer appears as a separate risk factor [{R}]. The export risk factor was "
            "removed [{K}]."),
        ("The Taiwan risk factor no longer appears as a separate risk factor [{R}], and the export risk factor "
            "was removed [{K}]."),
        ("The Taiwan risk factor no longer appears as a separate risk factor [{R}]; the export risk factor was "
            "dropped [{K}]."),
        ("The Taiwan risk factor no longer appears as a separate risk factor, and the export risk factor was "
            "reworded [{K}]."),
        ("The Taiwan risk factor no longer appears as a separate risk factor, and the export risk factor was "
            "reworded [{K}] [{R}]."),
        ("The NAC sentence's wording was not found in the newer filing [{K}]; the export risk factor itself "
            "was reworded [{NEWREW}]."),
        "Passages whose wording was not found in the newer filing: the NAC sentence [{K}].",
        "- Passages whose wording was not found in the newer filing: the NAC sentence [{K}]",
        "The Taiwan risk factor [{K}] no longer appears as a separate risk factor.",
        ("The Taiwan risk factor, which no longer appears as a separate risk factor [{K}], may be covered "
            "elsewhere."),
        "Two risk factors no longer appear as a separate risk factor: Taiwan [{R}] and China [{K}].",
        "The Taiwan risk factor no longer appears as a separate risk factor [{U}].",
    ]),
    ("independent-probe-batch-6-positive-claims", "removal", [
        "Nvidia removed nothing but the Taiwan risk factor [{K}].",
        "Nothing was removed but the Taiwan risk factor [{K}].",
        "Nvidia removed none but the Taiwan risk factor [{K}].",
        "Unlike Micron Nvidia dropped the Russia risk factor [{K}].",
        ("Nvidia did not reword the export risk factor and the sentence's wording was not found in the newer "
            "filing [{K}]."),
        "Nvidia did not show a change and one risk factor was removed [{K}].",
        "Nvidia added no new risk factors and the sentence's wording was not found in the newer filing [{K}].",
        ("Nvidia added no new risk factors because the sentence's wording was not found in the newer filing "
            "[{K}]."),
        ("Nvidia added no new risk factors, but the sentence's wording was not found in the newer filing "
            "[{K}]."),
        ("Nvidia added no new risk factors and, in the 10-K, the sentence's wording was not found in the newer "
            "filing [{K}]."),
        ("The comparison cannot reword the export risk factor and the sentence's wording was not found in the "
            "newer filing [{K}]."),
        "Removed risk factors:\nTaiwan risk factor [{K}].",
        "Removed risk factors:\n- Taiwan risk factor [{K}]",
        "Removed risk factors:\nNone found. Taiwan risk factor [{K}].",
    ]),
] for text in texts]

# documented limits: honest text the check still flags (the safe direction)
LIMIT_FP = [(family, context, t(text)) for family, context, texts in [
    # The prompt's hedge wording used to DEFINE itself or in a counterfactual: it names no item and cites no id, so it cannot be told
    # from an uncited claim, and the answer prompt reserves that wording for the ids of a removed list. The cost is one escalation.
    ("generic-definition-of-the-hedge-wording", "none", [
        ("Here, \"no longer appears as a separate risk factor\" means only that the text check found no matching "
            "text in the newer filing."),
        ("An item that \"no longer appears as a separate risk factor\" is one for which the text check found no "
            "matching text in the newer filing."),
        ("Being listed as no longer appearing as a separate risk factor does not mean the company removed the "
            "risk."),
        ("A risk factor that no longer appears as a separate risk factor may still be covered inside other "
            "risk factors, so absence from the list is not proof of removal."),
        ("When a risk factor no longer appears as a separate risk factor, parts of its content may be covered "
            "inside other risk factors."),
        ("The text check only looks for matching text; a passage whose wording was not found in the newer "
            "filing may still exist in different words."),
        ("Had any risk factor been removed, it would appear in the \"No longer appears as a separate risk "
            "factor\" list, which shows none found."),
        ("If a risk factor had been dropped, the text check would list it as no longer appearing as a separate "
            "risk factor; it lists none."),
    ]),
    # The prompt's hedge wording used to DEFINE itself or in a counterfactual: it names no item and cites no id, so it cannot be told
    # from an uncited claim, and the answer prompt reserves that wording for the ids of a removed list. The cost is one escalation.
    ("generic-definition-of-the-hedge-wording", "removal", [
        ("The export risk factor would have been listed under \"No longer appears as a separate risk factor\" if "
            "it had been dropped, but it is listed as reworded [{K}] [{NEWREW}]."),
    ]),
    # A cited bullet under a removed-list heading is judged by its id, however it is worded: the same shape as the pinned FLAG
    # '### L / - None found. / - Export risk [K]'.
    ("none-heading-with-a-cited-note-bullet", "new_reworded", [
        ("### No longer appears as a separate risk factor\n- None found for this comparison.\n- Note: the export "
            "risk factor was reworded, not removed [{K}]"),
    ]),
    # A cited sentence under a removed-list heading reads as an item of that list ('### L / The indebtedness risk factor [K].' is
    # pinned as a claim); only a body that cites nothing and excepts nothing is released as 'none found'.
    ("none-heading-then-cited-survival-prose", "removal", [
        ("### No longer appears as a separate risk factor\nNone. The export risk factor is still disclosed "
            "[{K}]."),
        ("### No longer appears as a separate risk factor\nThe text check found none for this comparison; the "
            "export risk factor was reworded and is still disclosed [{K}]."),
        ("### Passages whose wording was not found in the newer filing\nThe text check found none; every "
            "passage of the export risk factor [{K}] was matched or reworded."),
        ("**No longer appears as a separate risk factor:**\nNone found. The export risk factor is still "
            "disclosed, only reworded [{K}]."),
    ]),
    # A cited sentence under a removed-list heading reads as an item of that list ('### L / The indebtedness risk factor [K].' is
    # pinned as a claim); only a body that cites nothing and excepts nothing is released as 'none found'.
    ("none-heading-then-cited-survival-prose", "new_reworded", [
        ("### No longer appears as a separate risk factor\nThe text check found none. The only change in the "
            "other direction is a new risk factor, Sovereign AI [{N}]."),
    ]),
    # an "and" followed by a DETERMINER ('... and that statement were removed') has the shape of the pinned positive claim 'Nvidia
    # did not add risks and the export risk was removed [K]'. Where the conjunct has no determiner ('did not find that the export and
    # licensing risks were dropped') the negated evidence verb holds its subject noun phrase, and those rows pass; this one cannot be
    # told from the claim without a parser, and the safe direction is to flag.
    ("negated-noun-phrase-with-an-and-inside-it", "none", [
        "It does not mean the risk factor and that statement were removed.",
    ]),
    # an "and" followed by a DETERMINER ('... and that statement were removed') has the shape of the pinned positive claim 'Nvidia
    # did not add risks and the export risk was removed [K]'. Where the conjunct has no determiner ('did not find that the export and
    # licensing risks were dropped') the negated evidence verb holds its subject noun phrase, and those rows pass; this one cannot be
    # told from the claim without a parser, and the safe direction is to flag.
    ("negated-noun-phrase-with-an-and-inside-it", "removal", [
        "It does not mean the risk factor and that statement were removed [{K}].",
    ]),
    # A claim shares the ids of its sentence, so all of them must be removed ids: '..., and the export risk factor was reworded [R]
    # [K]' also cites a reworded item, and the check cannot tell which id stands behind which clause.
    ("one-claim-and-a-second-statement-citing-a-reworded-id", "removal", [
        ("The Taiwan risk factor no longer appears as a separate risk factor, and the export risk factor was "
            "reworded [{R}] [{K}]."),
    ]),
] for text in texts]

# documented limits: positive claims the check still lets through
LIMIT_FN = [(family, context, t(text)) for family, context, texts in [
    # 'It would be wrong to say that Nvidia did NOT remove X': two negations cancel, and the check has no parser to count them.
    ("double-negation", "removal", [
        "It would be wrong to say that Nvidia did not remove the indebtedness risk factor [{K}].",
    ]),
    # Words the removal lexicon leaves out on purpose ('disappeared', 'scrapped', 'left out', 'not carried forward'): each is also
    # risk-content prose in answers that have nothing to do with a comparison (the same reason 'absent' / 'missing' / 'cut' stay out).
    ("removal-lexicon-gap", "removal", [
        "The indebtedness risk factor disappeared from the newer 10-K [{K}].",
        "The indebtedness risk factor was not carried forward into the newer 10-K [{K}].",
        "Nvidia scrapped the indebtedness risk factor in the newer 10-K [{K}].",
        "The indebtedness risk factor was left out of the newer 10-K [{K}].",
    ]),
    # 'no comment about its decision TO DROP X': the verb is an infinitive after a decision noun, not the object of the act, and 'no
    # plans to remove the risk factor' is honest; the quantifier keeps its twelve-word reach here (the previous check did too).
    ("infinitive-after-a-quantified-communication-noun", "removal", [
        ("In the newer filing Nvidia offered no comment about its decision to drop the export risk factor "
            "[{K}]."),
    ]),
    # 'Its removal is the only change [id]': the clause names no disclosure and holds no verb, so it needs the sentence before it.
    ("removal-noun-in-a-clause-with-no-disclosure-noun", "removal", [
        "There was no rewording of the Russia risk factor. Its removal is the only change [{K}].",
    ]),
    # ', and only that one, was removed': the pinned PASS 'The export risk factor was reworded, and shares were removed from the
    # index [K]' has the same shape; a clause that only continues a sentence is read as a claim when a pronoun or which / and + an
    # auxiliary leads it.
    ("comma-and-clause-with-no-disclosure-noun", "removal", [
        "The Taiwan risk factor, and only that one, was removed [{K}].",
    ]),
] for text in texts]

def _ids(rows):
    return [f"{family}-{n}" for n, (family, _, _) in enumerate(rows)]


@pytest.mark.parametrize("family, context, text", PASS_CASES, ids=_ids(PASS_CASES))
def test_honest_text_is_not_flagged(family, context, text):
    assert claims(context, text) == (), text


@pytest.mark.parametrize("family, context, text", FLAG_CASES, ids=_ids(FLAG_CASES))
def test_a_positive_removal_claim_no_removed_list_supports_is_flagged(family, context, text):
    assert claims(context, text), text
    _, ctx, valid = CONTEXTS[context]()
    assert "unsupported_removal_claim" in verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=ctx,
                                                        sources=sources_from_context(ctx))


@pytest.mark.parametrize("family, context, text", LIMIT_FP, ids=_ids(LIMIT_FP))
def test_known_limit_honest_text_is_still_flagged(family, context, text):
    """Pinned so that a change of this behaviour is a decision, not an accident. The safe direction: it costs one escalation."""
    assert claims(context, text), text


@pytest.mark.parametrize("family, context, text", LIMIT_FN, ids=_ids(LIMIT_FN))
def test_known_limit_positive_claim_is_still_not_flagged(family, context, text):
    assert claims(context, text) == (), text

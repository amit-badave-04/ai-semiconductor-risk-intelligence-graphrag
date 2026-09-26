"""What the served temporal answer may say, per claim class, given the measured HELD-OUT precision (docs/v2/M1B_PLAN.md L.10).

The answer makes six kinds of claim about a pair of annual filings: an older risk factor was ``removed``, a newer one is ``new``, an
older one is ``unsettled`` (the text check could not decide), a passage inside a surviving risk factor was ``removed``, ``added`` or
``reworded``. ``build_narration`` turns the held-out numbers of ``scripts/verify_temporal.py`` into one table row per class:

    claim, unit, definition, k, n, precision, ci (Wilson 95%), gate, threshold, status, licensed_wording, plus ``unscored`` and
    ``breakdown`` where they exist.

Wording rule (no threshold beyond the plan's): a claim class whose precision PASSES the plan's gate (>= 0.90 on at least 5
predictions) may use the factual wording of M1B_PLAN L.7 / ``answer.txt``; one that FAILS or has INSUFFICIENT-DATA gets the plan's
fallback (M1B_PLAN B: "text changed" only; the plan states it per filer, it is applied here per claim class). Two classes have no plan threshold and are ``REPORTED``: an unsettled item asserts neither
removal nor presence (so it has no precision; the table shows what the unsettled items are in the gold), and a reworded passage,
which is scored on the older side's confusion matrix (unit: sentence).
"""

from collections.abc import Mapping

from .rates import MIN_POSITIVES, PASS, PRECISION_MIN, ci, rate_status, ratio

REPORTED = "REPORTED"
FALLBACK_SOURCE = "M1B_PLAN B fallback ('text changed' only), applied per claim class"
UNSETTLED_WORDING = ("Not matched: the text check could not verify whether this older risk factor still appears in the newer filing; "
                     "it is not verified as removed and not verified as present.")
REWORDED_WORDING = "The older and the newer wording of the same passage are shown side by side; the size of the change is not characterised."

# per gated class: unit, what the served answer claims, how k / n are counted, the wording a PASS licenses, and the fallback subject
CLAIMS = {
    "removed_item": {
        "unit": "item", "claim": "this older risk factor was removed: it is not disclosed anywhere in the newer risk section",
        "definition": "older items the alignment labelled removed: k of n are removed in the gold",
        "licensed": "Removed (text-verified): this risk factor of the older filing no longer appears in the newer filing's risk section.",
        "subject": "this risk factor's text", "verb": "removed"},
    "new_item": {
        "unit": "item", "claim": "this newer risk factor is new: it is not disclosed anywhere in the older risk section",
        "definition": "newer items the alignment labelled new: k of n are new in the gold",
        "licensed": "Added (text-verified): this risk factor is new in the newer filing's risk section.",
        "subject": "this risk factor's text", "verb": "new"},
    "removed_passage": {
        "unit": "passage", "claim": "this sentence of the older filing no longer appears in the newer risk section",
        "definition": "predicted removed passages of the sampled items: k of n cover mostly gold-removed sentences",
        "licensed": "This sentence of the older filing no longer appears in the newer filing's risk section (never: the company dropped the "
                    "risk factor, unless the whole item is removed).",
        "subject": "this passage's text", "verb": "removed"},
    "added_passage": {
        "unit": "passage", "claim": "this passage is new in the newer filing: it does not appear in the older risk section",
        "definition": "predicted added passages of the sampled items: k of n cover mostly gold-added sentences",
        "licensed": "This passage is new in the newer filing's risk section: it does not appear in the older filing's.",
        "subject": "this passage's text", "verb": "added"},
}
NOTES = (
    "Precision is measured on the held-out gold only and counts the predictions the gold can verify; 'unscored' predictions (no gold "
    "label, or a passage that covers no gold sentence) are not in it. A null 'unscored' means the report predates that count.",
    "Every gated row uses the plan's threshold (precision >= 0.90) and never passes on fewer than 5 predictions.",
    "The held-out split was inspected before the later alignment stages (see held_out_inspections): these are post-inspection "
    "numbers, not a blind test.",
    "Passages and sentences are correlated within a risk item, so the intervals (Wilson, independent trials) are too narrow for them.",
)


def fallback_wording(subject: str, verb: str) -> str:
    return f"'Text changed' only ({FALLBACK_SOURCE}): say that {subject} differs between the filings, not that it was {verb}."


def _row(key: str, **fields) -> dict:
    spec = CLAIMS.get(key, {})
    return {"claim": fields.pop("claim", spec.get("claim")), "unit": fields.pop("unit", spec.get("unit")),
            "definition": fields.pop("definition", spec.get("definition")), **fields}


def _gated_row(key: str, k: int, n: int, gate: str, **extra) -> dict:
    spec, status = CLAIMS[key], rate_status(k, n, PRECISION_MIN)
    wording = spec["licensed"] if status == PASS else fallback_wording(spec["subject"], spec["verb"])
    return _row(key, k=k, n=n, precision=ratio(k, n), ci=ci(k, n), gate=gate, threshold=PRECISION_MIN, status=status,
                licensed_wording=wording, **extra)


def _confusion_column(confusion: Mapping[str, Mapping[str, int]], predicted: str) -> dict[str, int]:
    """gold label -> number of gold sentences the alignment predicted as ``predicted`` (zero cells left out)."""
    return {gold: cell[predicted] for gold, cell in sorted(confusion.items()) if cell.get(predicted)}


def _item_row(key: str, block: Mapping, gate: str) -> dict:
    return _gated_row(key, block["tp"], block["tp"] + block["fp"], gate, unscored=block.get("unlabelled_positive"),
                      breakdown=None)


def _passage_row(key: str, block: Mapping, predicted: str, gate: str) -> dict:
    p = block["passage"]
    return _gated_row(key, p["tp"], p["tp"] + p["fp"], gate, unscored=p["unverifiable"],
                      breakdown={"sentences_predicted_" + predicted + "_by_gold_label": _confusion_column(block["confusion"], predicted)})


def _unsettled_row(block: Mapping) -> dict:
    n, removed = block["uncertain"] - block.get("unpredicted", 0), block.get("uncertain_gold_removed")     # a gold item with no decision is not served
    breakdown = None if removed is None else {"gold_removed": removed, "gold_still_present": n - removed}
    return _row("unsettled_item", unit="item", claim="this older risk factor is neither verified removed nor verified present",
                definition="older items left unsettled by the alignment; no precision (the claim asserts neither direction), the "
                           "breakdown shows what they are in the gold",
                k=None, n=n, precision=None, ci=None, gate=None, threshold=None, status=REPORTED, licensed_wording=UNSETTLED_WORDING,
                unscored=None, breakdown=breakdown)


def _reworded_row(block: Mapping) -> dict:
    column = _confusion_column(block["confusion"], "reworded")
    n, strict = sum(column.values()), column.get("reworded", 0)
    k = n - column.get("removed", 0)
    return _row("reworded_passage", unit="sentence",
                claim="the older and the newer wording of this passage are shown: the sentence still has a counterpart",
                definition="gold sentences covered by a reworded passage: k of n are not removed in the gold (the counterpart exists); "
                           "'strict' counts those the gold labels reworded (an edit, not a near copy)",
                k=k, n=n, precision=ratio(k, n), ci=ci(k, n), gate=None, threshold=None, status=REPORTED,
                licensed_wording=REWORDED_WORDING, unscored=None,
                breakdown={"sentences_predicted_reworded_by_gold_label": column,
                           "strict": {"k": strict, "n": n, "precision": ratio(strict, n), "ci": ci(strict, n)}})


def build_narration(item_level: Mapping, passage_level: Mapping) -> dict:
    """The narration table from the held-out blocks of a ``verify_temporal`` report (``item_level`` / ``passage_level``)."""
    removed, new = item_level["older_removed"]["held_out"], item_level["newer_new"]["held_out"]
    older, newer = passage_level["older"]["held_out"], passage_level["newer"]["held_out"]
    rows = {"removed_item": _item_row("removed_item", removed, "item_drop_precision_heldout"),
            "new_item": _item_row("new_item", new, "item_new_precision_heldout"),
            "unsettled_item": _unsettled_row(removed),
            "removed_passage": _passage_row("removed_passage", older, "removed", "passage_drop_precision_heldout"),
            "added_passage": _passage_row("added_passage", newer, "added", "passage_added_precision_heldout"),
            "reworded_passage": _reworded_row(older)}
    return {"split": "held_out", "threshold": PRECISION_MIN, "min_positives": MIN_POSITIVES, "fallback": FALLBACK_SOURCE,
            "order": list(rows), "rows": rows, "notes": list(NOTES)}


def format_narration(narration: Mapping) -> list[str]:
    """The table as printable lines (one per claim class)."""
    lines = [f"{'claim class':<18}{'k/n':>9}  {'precision [95% CI]':<22}{'status':<18}licensed wording"]
    for key in narration["order"]:
        r = narration["rows"][key]
        share = f"{'-' if r['k'] is None else r['k']}/{r['n']}"
        value = "  -  " if r["precision"] is None else f"{r['precision']:.3f} [{r['ci'][0]:.2f},{r['ci'][1]:.2f}]"
        lines.append(f"{key:<18}{share:>9}  {value:<22}{r['status']:<18}{r['licensed_wording']}")
    return lines

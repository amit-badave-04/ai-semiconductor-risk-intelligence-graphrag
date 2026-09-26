"""Source-text gold for "was this risk item still in the newer annual filing?".

The earlier calibration failed because its labellers saw only what the model saw, so a wrong "risk dropped"
answer looked right to them. Here a label must be PROVEN against the newer filing's full section text, by machine:

- a label that says the risk is present (``unchanged``/``reworded``/``merged``/``carried``) must carry a verbatim
  quote that literally occurs in the other filing's text (normalised for case, whitespace and quote marks);
- a label that says it is absent (``removed``/``new``) must list what was searched, and is contradicted outright
  when the item's own headline is still in the other text.

Around that: strict-majority consensus, agreement statistics (pairwise and Krippendorff's alpha), a tamper-evident
freeze, and scoring of any algorithm's predictions against the frozen gold (drop precision and recall).
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Mapping, Sequence

from ..extraction.gates import normalize

OLDER_LABELS = ("unchanged", "reworded", "merged", "removed")
NEWER_LABELS = ("carried", "new")
_PRESENT = {"older": {"unchanged", "reworded", "merged"}, "newer": {"carried"}}
_ABSENT = {"older": {"removed"}, "newer": {"new"}}
_PRESENT_FOR_SCORING = {"unchanged", "reworded", "merged"}
MIN_QUOTE_CHARS = 40       # a shorter quote proves nothing ("the company", "our stock")
MIN_SEARCH_TERMS = 3
_HEADLINE_FALLBACK_CHARS = 100   # paragraph units have no headline: use the opening of the text

LABELLING_INSTRUCTIONS = """\
You are labelling risk items from an annual report (the OLDER filing) against the FULL section text of the NEWER
annual report of the same company. For each older item decide, by reading the whole newer text, exactly one label:
- unchanged: the same text is in the newer filing. Give `quote`: at least 40 characters copied verbatim from the newer text.
- reworded: the same risk is still disclosed in edited wording. Give `quote`: the matching newer sentence(s), copied verbatim.
- merged: the risk was absorbed into another newer item or moved to another place. Give `quote`: the matching newer text, verbatim.
- removed: the risk is NOT disclosed anywhere in the newer section, not even briefly or inside a longer risk. Give
  `search_terms`: at least three distinctive words or phrases you searched the WHOLE newer text for, not just the same position.
Quotes must be copied exactly (case, whitespace and quote marks are normalised; no ellipses, no paraphrase). A quote that
is not found in the newer text, or a `removed` label whose headline is still in the newer text, is rejected by machine.
You never see any algorithm output, lineage id or another labeller's answer; do not guess what a system might have said.
Answer with a JSON list: [{"item_id": ..., "label": ..., "quote": ... | "search_terms": [...], "note": "optional"}].
"""


@dataclass(frozen=True)
class ValidatedAnnotation:
    accepted: dict[str, str]                 # item_id -> label
    rejected: list[tuple[str, str]]          # (item_id, reason)
    missing: list[str]                       # items without an accepted label, in item order


@dataclass(frozen=True)
class Aggregate:
    consensus: dict[str, str]
    needs_adjudication: list[str]
    pairwise_agreement: float
    alpha: float


@dataclass(frozen=True)
class Metrics:
    n: int
    true_drops: int
    false_drops: int
    missed_drops: int
    uncertain: int
    drop_precision: float | None
    drop_recall: float | None
    uncertain_rate: float


def _identity(item: Mapping) -> str:
    """The text that identifies an item inside the other filing: its headline, or the opening of a paragraph unit."""
    headline = normalize(item.get("headline") or "")
    return headline or normalize(item.get("text") or "")[:_HEADLINE_FALLBACK_CHARS]


def _reject_reason(label: Mapping, item: Mapping, other_norm: str, side: str) -> str | None:
    kind = label.get("label")
    if kind in _PRESENT[side]:
        quote = normalize(str(label.get("quote") or ""))
        if len(quote) < MIN_QUOTE_CHARS:
            return f"quote too short ({len(quote)} < {MIN_QUOTE_CHARS} characters)"
        if quote not in other_norm:
            return "quote not found in the source text"
        return None
    terms = [t for t in (label.get("search_terms") or []) if isinstance(t, str) and t.strip()]
    if len(terms) < MIN_SEARCH_TERMS:
        return f"search_terms needs at least {MIN_SEARCH_TERMS} entries"
    identity = _identity(item)
    if identity and identity in other_norm:
        return "headline is still in the source text, so the item cannot be absent"
    return None


def validate_annotation(labels: Sequence[Mapping], items: Sequence[Mapping], other_text: str, *,
                        side: str) -> ValidatedAnnotation:
    """Machine-check one annotator's labels for one pair. ``side`` is ``"older"`` (items from the older filing,
    ``other_text`` = the newer section) or ``"newer"`` (the reverse)."""
    if side not in ("older", "newer"):
        raise ValueError(f"side must be 'older' or 'newer', got {side!r}")
    allowed = OLDER_LABELS if side == "older" else NEWER_LABELS
    by_id = {i["item_id"]: i for i in items}
    other_norm = normalize(other_text)
    accepted: dict[str, str] = {}
    rejected: list[tuple[str, str]] = []
    for lab in labels:
        item_id = lab.get("item_id")
        if item_id not in by_id:
            rejected.append((str(item_id), "unknown item"))
        elif lab.get("label") not in allowed:
            rejected.append((item_id, f"unknown label {lab.get('label')!r} (allowed: {', '.join(allowed)})"))
        elif item_id in accepted:
            rejected.append((item_id, "duplicate label for the same item"))
        else:
            reason = _reject_reason(lab, by_id[item_id], other_norm, side)
            if reason:
                rejected.append((item_id, reason))
            else:
                accepted[item_id] = lab["label"]
    missing = [i["item_id"] for i in items if i["item_id"] not in accepted]
    return ValidatedAnnotation(accepted=accepted, rejected=rejected, missing=missing)


def majority_label(votes: Sequence[str]) -> str | None:
    """The label held by a strict majority of the votes, else None (the item goes to adjudication)."""
    if not votes:
        return None
    label, count = Counter(votes).most_common(1)[0]
    return label if count * 2 > len(votes) else None


def pairwise_agreement(annotations: Mapping[str, Mapping[str, str]]) -> float:
    """Pooled fraction of items on which two annotators agree, over all annotator pairs (vacuously 1.0 if none overlap)."""
    same = total = 0
    for a, b in combinations(annotations.values(), 2):
        for item in a.keys() & b.keys():
            total += 1
            same += a[item] == b[item]
    return same / total if total else 1.0


def krippendorff_alpha(annotations: Mapping[str, Mapping[str, str]]) -> float:
    """Krippendorff's alpha for nominal labels, tolerating missing labels (units with < 2 labels are ignored)."""
    units: dict[str, list[str]] = {}
    for ann in annotations.values():
        for unit, value in ann.items():
            units.setdefault(unit, []).append(value)
    coincidence: Counter = Counter()
    for values in units.values():
        m = len(values)
        if m < 2:
            continue
        for i, j in ((i, j) for i in range(m) for j in range(m) if i != j):
            coincidence[(values[i], values[j])] += 1 / (m - 1)
    totals: Counter = Counter()
    for (c, _k), weight in coincidence.items():
        totals[c] += weight
    n = sum(totals.values())
    if n <= 1:
        return 1.0
    observed = sum(w for (c, k), w in coincidence.items() if c != k)
    expected = sum(totals[c] * totals[k] for c in totals for k in totals if c != k) / (n - 1)
    return 1.0 if expected == 0 else 1.0 - observed / expected


def aggregate(annotations: Mapping[str, Mapping[str, str]]) -> Aggregate:
    """Consensus by strict majority, the items that need an adjudicator, and the agreement statistics."""
    votes: dict[str, list[str]] = {}
    for ann in annotations.values():
        for item, label in ann.items():
            votes.setdefault(item, []).append(label)
    consensus, disputed = {}, []
    for item in sorted(votes):
        label = majority_label(votes[item])
        if label is None:
            disputed.append(item)
        else:
            consensus[item] = label
    return Aggregate(consensus=consensus, needs_adjudication=disputed,
                     pairwise_agreement=pairwise_agreement(annotations), alpha=krippendorff_alpha(annotations))


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def freeze(gold: Mapping, path: Path | str) -> str:
    """Write ``gold`` plus its sha256 (over the canonical JSON of the gold itself) and return the hash.
    Freeze BEFORE tuning any threshold: the hash makes a later edit to the gold detectable."""
    if "sha256" in gold:
        raise ValueError("the gold must not already carry a 'sha256' key")
    digest = hashlib.sha256(_canonical(gold).encode("utf-8")).hexdigest()
    Path(path).write_text(json.dumps({**gold, "sha256": digest}, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    return digest


def verify_frozen(path: Path | str) -> bool:
    """True when the file's content still hashes to the sha256 recorded in it."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    recorded = doc.pop("sha256", None)
    return recorded == hashlib.sha256(_canonical(doc).encode("utf-8")).hexdigest()


def score_predictions(gold: Mapping[str, str], predicted: Mapping[str, str]) -> Metrics:
    """Score an algorithm's labels (item -> unchanged/reworded/merged/removed/uncertain) against the gold.

    A "drop" is ``removed``; unchanged/reworded/merged all mean the risk is still present. ``uncertain`` (or an item the
    algorithm did not label) is counted separately, not as a drop and not as a wrong answer: it is reported as a rate."""
    tp = fp = missed = uncertain = gold_drops = 0
    for item, truth in gold.items():
        pred = predicted.get(item, "uncertain")
        gold_drops += truth == "removed"
        if pred == "uncertain":
            uncertain += 1
        elif pred == "removed":
            tp += truth == "removed"
            fp += truth != "removed"
        elif truth == "removed" and pred in _PRESENT_FOR_SCORING:
            missed += 1
    n = len(gold)
    return Metrics(n=n, true_drops=tp, false_drops=fp, missed_drops=missed, uncertain=uncertain,
                   drop_precision=(tp / (tp + fp)) if (tp + fp) else None,
                   drop_recall=(tp / gold_drops) if gold_drops else None,
                   uncertain_rate=(uncertain / n) if n else 0.0)


def build_packet(pair: Mapping, older_items: Sequence[Mapping], newer_section_text: str) -> dict:
    """Everything a blind labeller gets: the older items, the FULL newer section text, and the rules. Nothing else."""
    return {"pair": dict(pair),
            "older_items": [{"item_id": i["item_id"], "headline": i.get("headline", ""), "text": i["text"]}
                            for i in older_items],
            "newer_section_text": newer_section_text,
            "instructions": LABELLING_INSTRUCTIONS}

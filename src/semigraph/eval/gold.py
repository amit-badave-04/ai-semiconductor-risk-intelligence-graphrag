"""Source-text gold for "was this risk item still in the newer annual filing?".

The earlier calibration failed because its labellers saw only what the model saw, so a wrong "risk dropped"
answer looked right to them. Here a label must be PROVEN against the newer filing's full section text, by machine:

- a label that says the risk is present (``unchanged``/``reworded``/``merged``/``carried``) must carry a verbatim
  quote that literally occurs in the other filing's text (normalised for case, whitespace and quote marks);
- a label that says it is absent (``removed``/``new``) must list what was searched, and is contradicted outright
  when the item's own headline is still in the other text.

Around that: strict-majority consensus, agreement statistics (pairwise and Krippendorff's alpha), a tamper-evident
freeze, and scoring of any algorithm's predictions against the frozen gold (drop precision and recall).

The same idea one level down (the SENTENCE layer, needed because a risk item can survive while single sentences of
it were removed): for a seeded sample of items, every sentence is labelled ``present`` / ``reworded`` / ``removed``
(``added`` on the newer side) against the other filing's full section text and machine-checked the same way
(``validate_sentence_annotation``). ``score_passages`` then grades an algorithm's changed PASSAGES against those
sentence labels. Nothing in this module ever sees algorithm output except ``score_passages``.
"""

import hashlib
import json
import random
from collections import Counter
from dataclasses import asdict, dataclass
from itertools import combinations
from operator import itemgetter
from pathlib import Path
from typing import Mapping, Sequence

from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

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


# ======================================================================================================================
# Sentence layer
# ======================================================================================================================

SENTENCE_LABELS_OLDER = ("present", "reworded", "removed")
SENTENCE_LABELS_NEWER = ("present", "reworded", "added")
_SENT_ABSENT = {"older": "removed", "newer": "added"}      # the label that says "not in the other filing"
_SENT_ELIGIBLE = {"older": frozenset({"unchanged", "reworded", "merged"}), "newer": frozenset({"carried"})}
_SENT_OPPOSITE = {"older": "added", "newer": "removed"}    # a passage kind that belongs to the other side
SENT_MIN_CHARS = 40             # a shorter fragment is not labelled (heading debris, list stubs)
SENT_MIN_QUOTE_CHARS = 30       # normalised; a shorter quote proves nothing
SENT_MIN_TERMS = 3              # distinct search terms behind a removed/added label
SENT_MIN_TERM_CHARS = 4
# rapidfuzz.fuzz.partial_ratio on normalised text (0-100). Measured on NVDA FY25 vs FY26: two UNRELATED sentences of the
# same filing score median 46, p99 56, max 61; so 70 separates "the same sentence" from noise and 45 (reworded) does not.
SENT_PRESENT_MIN_SIM = 70       # `present`: the quote must share content with the sentence
SENT_REWORDED_MIN_SIM = 45      # `reworded`: same check, looser (a rewording shares fewer characters)
SENT_VERBATIM_SIM = 92          # at or above this a quote/alignment is a copy: `reworded` and `removed` are contradicted
SENTENCE_SAMPLE_SEED = 20260926
SENTENCE_SAMPLE_K = 6
SENTENCE_SAMPLE_CAP = 320

_SENTENCE_TEMPLATE = """\
You are labelling single sentences taken from risk items of the {mine} annual report against the FULL section text of \
the {other} annual report of the same company. Read the whole {other} text: a sentence can be anywhere in it, not only \
at the same position or inside the same risk item. For each sentence decide exactly one label:
- present: the sentence, or a near-verbatim version of it, is in the {other} text. Give `quote`: at least 30 characters \
copied verbatim from the {other} text that carry the sentence's content. An edit that only changes the tense or a \
number ("impact" -> "impacted", "$4 billion" -> "$5 billion") leaves the sentence `present`, and so does moving it into \
another risk factor or another paragraph.
- reworded: the same statement is still made in the {other} text but in differently worded text, not a near copy. Give \
`quote`: the matching {other} text (at least 30 characters), copied verbatim. A quote that is a near-verbatim copy of the \
sentence makes the label `present`, not `reworded`.
- {absent}: the statement is NOT made anywhere in the {other} text, not even briefly or inside a longer passage. Give \
`search_terms`: at least 3 distinct words or phrases, each at least 4 characters, that you searched the WHOLE {other} \
text for. The label is rejected by machine when the sentence, or a near copy of it, is still in the {other} text.
When you are unsure between `{absent}` and `reworded`, you must search the other text again (different key nouns, names, \
numbers, defined terms) before you choose `{absent}`.
Quotes must be copied exactly (case, whitespace and quote marks are normalised; no ellipses, no paraphrase); a quote that \
is not found in the {other} text, or that is unrelated to the sentence, is rejected by machine.
You never see any algorithm output, lineage id or another labeller's answer; do not guess what a system might have said.
Label every sentence. Answer with a JSON list: [{"sentence_id": ..., "label": ..., "quote": ... | "search_terms": [...], \
"note": "optional"}].
"""


def _check_side(side: str) -> None:
    if side not in ("older", "newer"):
        raise ValueError(f"side must be 'older' or 'newer', got {side!r}")


def sentence_instructions(side: str = "older") -> str:
    """The blind-labelling rules for one side: ``older`` labels the older filing's sentences (present/reworded/removed)
    against the newer section; ``newer`` labels the newer filing's sentences (present/reworded/added) against the older."""
    _check_side(side)
    mine, other = ("OLDER", "NEWER") if side == "older" else ("NEWER", "OLDER")
    return (_SENTENCE_TEMPLATE.replace("{mine}", mine).replace("{other}", other)      # not str.format: the JSON example has braces
            .replace("{absent}", _SENT_ABSENT[side]))


SENTENCE_LABELLING_INSTRUCTIONS = sentence_instructions("older")


def sentence_similarity(a: str, b: str) -> float:
    """Content overlap (0-100) of two texts: ``partial_ratio`` of the normalised strings (K.2: with the rapidfuzz
    default processor, which also drops punctuation)."""
    return float(fuzz.partial_ratio(normalize(a), normalize(b), processor=default_process))


def item_sentences(item: Mapping, *, min_chars: int = SENT_MIN_CHARS) -> list[dict]:
    """The labellable sentences of one item. Ids number the RAW split (0-based), so they survive a change of
    ``min_chars``; ``start``/``end`` are offsets inside the item text, ``section_start``/``section_end`` offsets inside the
    section text (None when the item carries no ``char_start``)."""
    from ..graph.alignment import split_sentences      # lazy: alignment pulls in numpy/scipy, the gold must not need them

    text, base, item_id = item["text"], item.get("char_start"), item["item_id"]
    out = []
    for n, (start, end) in enumerate(split_sentences(text)):
        if end - start < min_chars:
            continue
        out.append({"sentence_id": f"{item_id}#s{n:03d}", "item_id": item_id, "text": text[start:end],
                    "start": start, "end": end,
                    "section_start": None if base is None else base + start,
                    "section_end": None if base is None else base + end})
    return out


def _sentence_entry_problem(entry, by_id: Mapping, accepted: Mapping, allowed: Sequence[str]) -> tuple[str, str | None]:
    """(id used for reporting, reason the entry is structurally invalid or None)."""
    if not isinstance(entry, Mapping):
        return "?", "entry is not an object"
    sid = entry.get("sentence_id")
    if sid is None:
        return "?", "missing field 'sentence_id'"
    if not isinstance(sid, str) or sid not in by_id:
        return str(sid), "unknown sentence"
    if entry.get("label") is None:
        return sid, "missing field 'label'"
    if entry["label"] not in allowed:
        return sid, f"unknown label {entry['label']!r} (allowed: {', '.join(allowed)})"
    if sid in accepted:
        return sid, "duplicate label for the same sentence"
    return sid, None


def _distinct_terms(raw) -> list[str] | None:
    """Search terms normalised, at least ``SENT_MIN_TERM_CHARS`` long and distinct (None when not a list)."""
    if not isinstance(raw, (list, tuple)):
        return None
    terms: list[str] = []
    for term in raw:
        norm = normalize(term) if isinstance(term, str) else ""
        if len(norm) >= SENT_MIN_TERM_CHARS and norm not in terms:
            terms.append(norm)
    return terms


def _absent_problem(entry: Mapping, sentence_norm: str, other_norm: str) -> str | None:
    """Reason an absent-label (removed / added) is rejected, or None. Verbatim containment first: alignment of a needle
    over 64 characters runs non-exhaustively (plan K.3), so it only adds the near-copy screen."""
    terms = _distinct_terms(entry.get("search_terms"))
    if terms is None:
        return "missing field 'search_terms'" if entry.get("search_terms") is None else "search_terms must be a list of strings"
    if len(terms) < SENT_MIN_TERMS:
        return (f"search_terms needs at least {SENT_MIN_TERMS} distinct entries of {SENT_MIN_TERM_CHARS}+ characters "
                f"(got {len(terms)})")
    if sentence_norm in other_norm:
        return "sentence is still in the source text (verbatim), so it cannot be absent"
    aligned = fuzz.partial_ratio_alignment(sentence_norm, other_norm, processor=default_process)
    if aligned is not None and aligned.score >= SENT_VERBATIM_SIM:
        return (f"a near-verbatim copy of the sentence is in the source text (partial_ratio {aligned.score:.0f} >= "
                f"{SENT_VERBATIM_SIM}), so it cannot be absent")
    return None


def _quote_problem(entry: Mapping, kind: str, sentence_norm: str, other_norm: str) -> str | None:
    """Reason a present/reworded label's quote is rejected, or None."""
    raw = entry.get("quote")
    if not isinstance(raw, str) or not raw.strip():
        return "missing field 'quote'"
    quote = normalize(raw)
    if len(quote) < SENT_MIN_QUOTE_CHARS:
        return f"quote too short ({len(quote)} < {SENT_MIN_QUOTE_CHARS} characters)"
    if quote not in other_norm:
        return "quote not found in the source text"
    sim = fuzz.partial_ratio(sentence_norm, quote, processor=default_process)
    floor = SENT_PRESENT_MIN_SIM if kind == "present" else SENT_REWORDED_MIN_SIM
    if sim < floor:
        return f"quote is not related to the sentence (partial_ratio {sim:.0f} < {floor})"
    if kind == "reworded" and sim >= SENT_VERBATIM_SIM:
        return (f"quote is a near-verbatim copy of the sentence (partial_ratio {sim:.0f} >= {SENT_VERBATIM_SIM}): "
                "that is `present`, not `reworded`")
    return None


def validate_sentence_annotation(labels: Sequence, sentences: Sequence[Mapping], other_text: str, *,
                                 side: str = "older") -> ValidatedAnnotation:
    """Machine-check one annotator's SENTENCE labels. ``sentences`` carry ``sentence_id`` and ``text``; ``other_text`` is
    the other filing's full section (the newer one for ``side="older"``). ``accepted`` maps sentence_id -> label."""
    _check_side(side)
    allowed = SENTENCE_LABELS_OLDER if side == "older" else SENTENCE_LABELS_NEWER
    by_id = {s["sentence_id"]: s for s in sentences}
    other_norm = normalize(other_text)
    accepted: dict[str, str] = {}
    rejected: list[tuple[str, str]] = []
    for entry in labels:
        sid, problem = _sentence_entry_problem(entry, by_id, accepted, allowed)
        if problem is None:
            sentence_norm = normalize(by_id[sid]["text"])
            problem = (_absent_problem(entry, sentence_norm, other_norm) if entry["label"] == _SENT_ABSENT[side]
                       else _quote_problem(entry, entry["label"], sentence_norm, other_norm))
        if problem:
            rejected.append((sid, problem))
        else:
            accepted[sid] = entry["label"]
    missing = [s["sentence_id"] for s in sentences if s["sentence_id"] not in accepted]
    return ValidatedAnnotation(accepted=accepted, rejected=rejected, missing=missing)


def _largest(items) -> Mapping:
    return min(items, key=lambda i: (-len(i["text"]), i["item_id"]))


def _eligibility(item_id: str, consensus: Mapping[str, str], votes: Mapping, side: str) -> str | None:
    """Why an item may be sampled: ``consensus`` (its item-level label is a present label) or ``vote_majority`` (the labellers
    split among labels so there is no consensus, but a strict majority of the votes say still present), else None.
    Deciding at present-versus-absent granularity keeps a heavily edited, hotly disputed item (the NVDA export-control
    risk factor) in the sample instead of letting a 3-way reworded/merged/unchanged split silently drop it."""
    eligible = _SENT_ELIGIBLE[side]
    label = consensus.get(item_id)
    if label is not None:
        return "consensus" if label in eligible else None
    ballots = votes.get(item_id) or {}
    ballots = list(ballots.values()) if isinstance(ballots, Mapping) else list(ballots)
    return "vote_majority" if ballots and 2 * sum(b in eligible for b in ballots) > len(ballots) else None


def _apply_cap(chosen: list, flagship, max_sentences: int) -> tuple[list, list[dict], int]:
    """Drop the smallest non-flagship items until the total fits; returns (kept, dropped records, total sentences)."""
    total = sum(len(s) for _, s in chosen)
    dropped = []
    for entry in sorted((c for c in chosen if c is not flagship), key=lambda c: (len(c[0]["text"]), c[0]["item_id"])):
        if total <= max_sentences:
            break
        total -= len(entry[1])
        dropped.append({"item_id": entry[0]["item_id"], "chars": len(entry[0]["text"]), "n_sentences": len(entry[1])})
    gone = {d["item_id"] for d in dropped}
    return [c for c in chosen if c[0]["item_id"] not in gone], dropped, total


def sentence_sample(items: Sequence[Mapping], item_consensus: Mapping[str, str], *, pair_id: str, side: str = "older",
                    item_votes: Mapping[str, Mapping[str, str] | Sequence[str]] | None = None,
                    seed: int = SENTENCE_SAMPLE_SEED, k: int = SENTENCE_SAMPLE_K,
                    max_sentences: int = SENTENCE_SAMPLE_CAP, min_chars: int = SENT_MIN_CHARS) -> dict:
    """Seeded sample of items whose sentences get labelled. Uses ONLY the item texts and the item-level GOLD labels.

    Eligible: older side ``unchanged``/``reworded``/``merged`` (never ``removed``), newer side ``carried`` (never ``new``),
    from the item consensus or, for an item without consensus, a strict majority of its ``item_votes`` (see
    ``_eligibility``), and at least one sentence of ``min_chars``. The largest eligible item (the NVDA export-control risk
    factor) is always included and counts towards ``k``; the rest is ``random.Random(f"{seed}|{pair_id}|{side}")`` over the
    eligible items in item_id order (the manifest lists them all, so the draw is reproducible from it). When the total
    exceeds ``max_sentences`` the smallest non-flagship items are dropped first and listed; a flagship that alone exceeds
    the cap is kept and flagged."""
    _check_side(side)
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    candidates, basis = [], {}
    for item in sorted(items, key=itemgetter("item_id")):
        why = _eligibility(item["item_id"], item_consensus, item_votes or {}, side)
        sentences = item_sentences(item, min_chars=min_chars) if why else []
        if sentences:
            candidates.append((item, sentences))
            basis[item["item_id"]] = why
    rng_key = f"{seed}|{pair_id}|{side}"
    overall = _largest(items)["item_id"] if items else None
    flagship = min(candidates, key=lambda c: (-len(c[0]["text"]), c[0]["item_id"])) if candidates else None
    chosen = []
    if flagship:
        pool = [c for c in candidates if c is not flagship]
        chosen = [flagship] + random.Random(rng_key).sample(pool, min(k - 1, len(pool)))
    kept, dropped, total = _apply_cap(chosen, flagship, max_sentences)
    flagship_id = flagship[0]["item_id"] if flagship else None
    note = None
    if flagship_id and overall != flagship_id:
        note = (f"the largest item ({overall}) is not eligible (item-level label {item_consensus.get(overall)!r}, "
                "or no labellable sentence); the largest eligible item is used")
    return {"pair_id": pair_id, "side": side, "seed": seed, "rng_key": rng_key, "k": k, "max_sentences": max_sentences,
            "min_chars": min_chars, "n_eligible_items": len(candidates), "eligible_item_ids": sorted(basis),
            "eligibility_basis": basis, "flagship_item_id": flagship_id,
            "overall_largest_item_id": overall, "flagship_note": note,
            "flagship_exceeds_cap": bool(flagship) and total > max_sentences, "dropped_items": dropped,
            "n_sentences": total,
            "items": [{"item_id": item["item_id"], "headline": item.get("headline") or "",
                       "char_start": item.get("char_start"),
                       "char_end": None if item.get("char_start") is None else item["char_start"] + len(item["text"]),
                       "sentences": sentences} for item, sentences in sorted(kept, key=lambda c: c[0]["item_id"])]}


def build_sentence_packet(meta: Mapping, sampled_items: Sequence[Mapping], other_section_text: str, *,
                          side: str | None = None) -> dict:
    """What a blind sentence labeller gets: the sampled items' sentences (id and text only), the FULL other-filing
    section text, and the rules. No item-level label, offset, seed or sampling detail is included."""
    side = side or meta.get("side") or "older"
    _check_side(side)
    return {"pair": dict(meta), "side": side,
            "items": [{"item_id": i["item_id"], "headline": i.get("headline") or "",
                       "sentences": [{"sentence_id": s["sentence_id"], "text": s["text"]} for s in i["sentences"]]}
                      for i in sampled_items],
            "other_section_text": other_section_text,
            "instructions": sentence_instructions(side)}


def sentence_spans(sample: Mapping) -> dict[str, list]:
    """sentence_id -> [item_id, section_start, section_end] for every sampled sentence (what scoring needs)."""
    return {s["sentence_id"]: [s["item_id"], s["section_start"], s["section_end"]]
            for item in sample["items"] for s in item["sentences"]}


def gold_sentence_records(entry: Mapping) -> list[dict]:
    """A frozen ``sentences`` entry (``labels`` + ``spans``) as the records ``score_passages`` takes, in id order."""
    records = []
    for sid in sorted(entry["labels"]):
        span = (entry.get("spans") or {}).get(sid)
        if not span:
            raise ValueError(f"no span recorded for sentence {sid}: it cannot be scored")
        records.append({"sentence_id": sid, "item_id": span[0], "section_start": span[1], "section_end": span[2],
                        "label": entry["labels"][sid]})
    return records


# --- scoring an algorithm's changed passages against the sentence gold ------------------------------------------------

_COVER = 0.5          # a gold sentence belongs to a passage when at least this share of it lies inside the passage


@dataclass(frozen=True)
class PassageMetrics:
    """Passage change layer vs the sentence gold, restricted to the SAMPLED items (the only place gold exists).

    ``positive_kind`` is the class being scored (``removed`` on the older side, ``added`` on the newer). ``reworded`` is a
    third class: it never counts as a hit, and shows up in ``confusion`` (gold label -> predicted label -> count, where a
    sentence no passage covers is predicted ``present``). ``uncertain`` sentences (covered by a passage of any other kind)
    are reported as a rate and, as in ``score_predictions``, are neither a drop nor a wrong answer, but a gold positive
    that is uncertain still counts against recall. Known asymmetry: ``graph/passages.py`` reports rewordings from the
    OLDER side only (a ``reworded`` passage carries the older item id), so on the newer side a gold-``reworded``
    sentence normally shows up as predicted ``present`` in ``confusion``; that is the contract, not a bug."""
    side: str
    positive_kind: str
    n_gold_sentences: int
    n_gold_positive: int
    sentence: dict            # tp, fp, fn, precision, recall
    passage: dict             # n_predicted, tp, fp, unverifiable, precision, n_gold_runs, recalled, recall
    confusion: dict
    uncertain: int
    uncertain_rate: float
    ignored_passages: int     # passages outside the sampled items, or of the other side's kind
    false_positive_ids: list
    false_negative_ids: list

    def to_dict(self) -> dict:
        return asdict(self)


def _passage_view(p) -> tuple[str, str, int, int]:
    values = []
    for name in ("kind", "item_id", "char_start", "char_end"):
        value = p.get(name) if isinstance(p, Mapping) else getattr(p, name, None)
        if value is None:
            raise ValueError(f"passage is missing {name!r}")
        values.append(value)
    return tuple(values)  # type: ignore[return-value]


def _check_gold_records(records: Sequence[Mapping], positive: str) -> None:
    for g in records:
        if g.get("section_start") is None or g.get("section_end") is None:
            raise ValueError(f"gold sentence {g.get('sentence_id')} has no section offsets: it cannot be scored")
        if g.get("label") not in ("present", "reworded", positive):
            raise ValueError(f"gold sentence {g.get('sentence_id')} has an unknown label {g.get('label')!r} "
                             f"(allowed: present, reworded, {positive})")


def _best_passage(g: Mapping, spans: Sequence[tuple[int, int, int]]) -> int | None:
    """Index of the passage holding most of gold sentence ``g`` (at least ``_COVER`` of it), else None."""
    length = g["section_end"] - g["section_start"]
    best, best_share = None, 0.0
    for idx, start, end in spans:
        share = (min(end, g["section_end"]) - max(start, g["section_start"])) / length if length > 0 else 0.0
        if share >= _COVER and share > best_share:
            best, best_share = idx, share
    return best


def _gold_runs(records: Sequence[Mapping], positive: str) -> list[list[str]]:
    """Maximal runs of consecutive gold-positive sentences (consecutive in an item's gold order)."""
    runs, current, last_item = [], [], None
    for g in sorted(records, key=lambda r: (r["item_id"], r["section_start"], r["sentence_id"])):
        if g["item_id"] != last_item or g["label"] != positive:
            if current:
                runs.append(current)
            current = []
        if g["label"] == positive:
            current.append(g["sentence_id"])
        last_item = g["item_id"]
    if current:
        runs.append(current)
    return runs


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def score_passages(predicted_passages: Sequence, gold_sentences: Sequence[Mapping], *, side: str = "older") -> PassageMetrics:
    """Grade predicted passages (``Passage`` objects or dicts with ``kind``, ``item_id``, ``char_start``, ``char_end``,
    section-text offsets) against gold sentence records (``sentence_id``, ``item_id``, ``section_start``,
    ``section_end``, ``label``; see ``gold_sentence_records``).

    A gold sentence is assigned to the passage that holds most of it (at least half of its characters, same item).
    Sentence level: tp = gold positive and predicted positive; fp = predicted positive but gold is present/reworded;
    fn = every gold positive not a tp. Passage level: a predicted positive passage is a tp when a strict majority of
    the gold sentences it covers are gold positive (fp otherwise; a passage covering no gold sentence is
    ``unverifiable`` and left out of precision); a maximal run of consecutive gold-positive sentences is recalled when a
    strict majority of its sentences is predicted positive, even if by several passages."""
    _check_side(side)
    positive, opposite = _SENT_ABSENT[side], _SENT_OPPOSITE[side]
    _check_gold_records(gold_sentences, positive)
    sampled = {g["item_id"] for g in gold_sentences}
    views = sorted((_passage_view(p) for p in predicted_passages), key=lambda v: (v[1], v[2], v[3], v[0]))
    live = [(kind, item, start, end) for kind, item, start, end in views if item in sampled and kind != opposite]
    by_item: dict[str, list[tuple[int, int, int]]] = {}
    for idx, (_kind, item, start, end) in enumerate(live):
        by_item.setdefault(item, []).append((idx, start, end))
    predicted: dict[str, str] = {}
    covered: dict[int, list[str]] = {}
    for g in gold_sentences:
        idx = _best_passage(g, by_item.get(g["item_id"], ()))
        kind = None if idx is None else live[idx][0]
        predicted[g["sentence_id"]] = ("present" if kind is None else positive if kind == positive
                                       else "reworded" if kind == "reworded" else "uncertain")
        if idx is not None:
            covered.setdefault(idx, []).append(g["sentence_id"])
    label = {g["sentence_id"]: g["label"] for g in gold_sentences}
    confusion: dict[str, dict[str, int]] = {}
    for sid, truth in label.items():
        cell = confusion.setdefault(truth, {})
        cell[predicted[sid]] = cell.get(predicted[sid], 0) + 1
    tp = sum(1 for sid in label if label[sid] == positive and predicted[sid] == positive)
    fp_ids = sorted(sid for sid in label if label[sid] != positive and predicted[sid] == positive)
    n_positive = sum(1 for truth in label.values() if truth == positive)
    fn_ids = sorted(sid for sid in label if label[sid] == positive and predicted[sid] != positive)
    uncertain = sum(1 for sid in label if predicted[sid] == "uncertain")
    positive_passages = [idx for idx, v in enumerate(live) if v[0] == positive]
    p_tp = p_fp = p_unverifiable = 0
    for idx in positive_passages:
        sids = covered.get(idx, [])
        if not sids:
            p_unverifiable += 1
        elif 2 * sum(1 for sid in sids if label[sid] == positive) > len(sids):
            p_tp += 1
        else:
            p_fp += 1
    runs = _gold_runs(gold_sentences, positive)
    recalled = sum(1 for run in runs if 2 * sum(1 for sid in run if predicted[sid] == positive) > len(run))
    return PassageMetrics(
        side=side, positive_kind=positive, n_gold_sentences=len(label), n_gold_positive=n_positive,
        sentence={"tp": tp, "fp": len(fp_ids), "fn": len(fn_ids), "precision": _ratio(tp, tp + len(fp_ids)),
                  "recall": _ratio(tp, n_positive)},
        passage={"n_predicted": len(positive_passages), "tp": p_tp, "fp": p_fp, "unverifiable": p_unverifiable,
                 "precision": _ratio(p_tp, p_tp + p_fp), "n_gold_runs": len(runs), "recalled": recalled,
                 "recall": _ratio(recalled, len(runs))},
        confusion=confusion, uncertain=uncertain, uncertain_rate=uncertain / len(label) if label else 0.0,
        ignored_passages=len(views) - len(live), false_positive_ids=fp_ids, false_negative_ids=fn_ids)


def must_hit(passages: Sequence, needles: Sequence[str], *, kind: str = "removed") -> dict[str, bool]:
    """For each named needle (a distinctive substring such as "Notified Advanced Computing"), whether a passage of
    ``kind`` (default ``removed``) contains it, compared case/whitespace/quote-normalised. Returns every needle so a
    miss is visible: ``all(must_hit(...).values())`` is the gate."""
    texts = [normalize(str(p.get("text") if isinstance(p, Mapping) else getattr(p, "text", "")) or "")
             for p in passages if (p.get("kind") if isinstance(p, Mapping) else getattr(p, "kind", None)) == kind]
    return {needle: bool(normalize(needle)) and any(normalize(needle) in text for text in texts) for needle in needles}

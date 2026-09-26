"""INSTRUMENT ACCEPTANCE TEST for the correctness judge (docs/v2/M1B_PLAN.md section E).

The judge is an instrument, and changing an instrument after seeing its results invites leniency shopping. The judge is therefore
accepted only if it passes a frozen, hash-pinned set of ADVERSARIAL PROBES (``artifacts/gold/judge_probes.json``) that the owner can
audit: answers whose correctness against the grading notes is a fact, not a taste.

  * expected_correct = false: the two served v1 answers that were WRONG (T1, T3), deployed answers that answer the wrong question or
    the wrong filing pair (T7, T12), and synthetic answers with exactly ONE defect each (a removal the notes contradict, a removal
    the notes list but the answer denies, a "carried over unchanged" claim for a new item, ...);
  * expected_correct = true: real deployed answers that are consistent with the notes, hedged wording included ("no matching risk
    factor found ... (new or restructured)", "wording was not found ... a differently worded version may exist").

    PYTHONPATH=src python scripts/judge_acceptance.py               # DRY RUN (default): prompt lengths and the cost, calls nothing
    PYTHONPATH=src python scripts/judge_acceptance.py --go          # PAID: every probe x --votes (3), majority per probe
    PYTHONPATH=src python scripts/judge_acceptance.py --show-prompts   # dry run that also prints every prompt

The paid run goes through ``eval/bakeoff.py::judge_open``, the very function the benchmark scoring uses, so it tests the production
path and not a second loop; every item and row is keyed by the PROBE id (probes on the same question do not collide). Grading notes
come from the benchmark's ``judge_notes`` (``notes_from: {"benchmark": id}``, the probe's question must equal the benchmark's), and,
for the two legacy probes, from the ``legacy_notes`` of ``artifacts/gold/temporal_questions.json`` (built ONLY from the frozen
source-text gold; the temporal file must carry the sha256 of the frozen gold on disk, else the script refuses; ``--notes benchmark``
reads the merged benchmark's T1/T3 notes for the legacy probes instead, equal once merged).

The probes file is refused unless (1) its content still hashes to the sha256 stored in it, (2) that hash equals
``PINNED_PROBES_SHA256`` in this script (a file rewritten together with its own hash is caught by the pin), (3) it is well formed
and holds at least one expected-correct and one expected-incorrect probe (a one-sided set would pass a judge that grades everything
the same way). ``--allow-unpinned-probes`` skips (2) for a new probe set under review; the report then says ``probes_pinned: false``.

Verdict (majority of the votes per probe, as in ``judge_open``):
  PASS          every expected-incorrect probe is graded incorrect AND every expected-correct probe correct, no judge call errored;
  FAIL          otherwise; LENIENCY failures (a wrong answer graded correct: the serious one) are listed first, then STRICTNESS
                failures (a consistent answer graded incorrect);
  INCONCLUSIVE  a judge call errored (an errored vote counts as "not correct" inside ``judge_open``, which for the expected-incorrect
                probes would be a false PASS), so any error blocks the verdict.

Exit codes: 0 PASS; 1 FAIL; 2 unusable input (missing/edited/unpinned probes, notes that do not match the frozen gold, ...);
3 INCONCLUSIVE. The report (artifacts/judge_acceptance.json) records per-probe votes and reasons, the judge prompt sha256 and its
version (``runner.JUDGE_PROMPT_VERSION``), the probes-file sha256, the model and the as-of date. A PASS proves the removal, hedge
and filing-pair rules on these probes; it does not prove figure checking (the notes carry no financial figures).
"""

import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")   # before any semigraph import: no remote cost-map fetch when litellm loads

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from semigraph.eval import bakeoff, gold, runner

DEFAULT_PROBES = Path("artifacts/gold/judge_probes.json")
DEFAULT_TEMPORAL = Path("artifacts/gold/temporal_questions.json")
DEFAULT_GOLD = Path("artifacts/gold/risk_items_gold.json")
DEFAULT_BENCHMARK = Path("src/semigraph/artifacts/benchmark.json")
DEFAULT_REPORT = Path("artifacts/judge_acceptance.json")
# ``gold.verify_frozen`` trusts the hash stored INSIDE a file, so a probe file rewritten together with its hash would pass it. The
# accepted probe set is therefore pinned here, exactly as the gold files are pinned in ``gold.KNOWN_GOLD_SHA256``. Changing a probe
# is a reviewed edit of this constant in the commit that changes the file.
PINNED_PROBES_SHA256 = "997ea65fbce48b8fb0168066016d340ab8ef1f5323e63246f8850f1600d24ba9"   # 12 probes: real-T5 dropped after review
VOTES = 3
PROBE_KEYS = ("id", "notes_from", "question", "answer", "valid_ids", "expected_correct", "basis")
NOTE_SOURCES = ("benchmark", "temporal_legacy")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXIT_PASS, EXIT_FAIL, EXIT_UNUSABLE, EXIT_INCONCLUSIVE = 0, 1, 2, 3
BOUND_PROMPT_CHARS = 8000       # the bake-off's per-call bound (JUDGE_CALL_USD) assumes about 2k tokens in, 300 out


# --- the probes file -------------------------------------------------------------------------------------------------------

def probes_sha256(doc: dict) -> str:
    """The gold-file convention: sha256 of the canonical JSON of the whole document without its own ``sha256`` key."""
    body = {k: v for k, v in doc.items() if k != "sha256"}
    return hashlib.sha256(gold._canonical(body).encode("utf-8")).hexdigest()


def _probe_problem(probe: object) -> str | None:
    if not isinstance(probe, dict):
        return "is not an object"
    missing = [k for k in PROBE_KEYS if k not in probe]
    if missing:
        return f"lacks {', '.join(missing)}"
    for key in ("id", "question", "answer", "basis"):
        if not isinstance(probe[key], str) or not probe[key].strip():
            return f"has an empty or non-text {key}"
    if not isinstance(probe["expected_correct"], bool):
        return "expected_correct is not true/false"
    ids = probe["valid_ids"]
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return "valid_ids is not a list of ids"
    src = probe["notes_from"]
    if not (isinstance(src, dict) and len(src) == 1 and next(iter(src)) in NOTE_SOURCES and isinstance(next(iter(src.values())), str)):
        return f"notes_from must be exactly one of {list(NOTE_SOURCES)} mapped to a question id"
    return None


def validate_probes(probes: object) -> None:
    """Refuse a malformed set, or one that cannot detect both failure directions."""
    if not isinstance(probes, list) or not probes:
        raise ValueError("the probes file holds no probes")
    seen: set[str] = set()
    for i, probe in enumerate(probes):
        problem = _probe_problem(probe)
        if problem:
            raise ValueError(f"probe #{i + 1} ({probe.get('id') if isinstance(probe, dict) else '?'}) {problem}")
        if probe["id"] in seen:
            raise ValueError(f"duplicate probe id {probe['id']}: verdicts are keyed by probe id")
        seen.add(probe["id"])
    polarities = {p["expected_correct"] for p in probes}
    if polarities != {True, False}:
        raise ValueError("the probes need at least one expected-correct and one expected-incorrect probe: a one-sided set would "
                         "accept a judge that grades every answer the same way")


def load_probes(path: Path, *, pin: str | None) -> dict:
    """The verified probes document; refuses an edited file (hash), an unpinned one (``pin``, None = skip) or a malformed one."""
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist (the frozen probes file: artifacts/gold/judge_probes.json)")
    doc = json.loads(path.read_text(encoding="utf-8"))
    recorded = doc.get("sha256")
    if not recorded or recorded != probes_sha256(doc):
        raise ValueError(f"{path} no longer hashes to the sha256 recorded in it: the probes were edited after they were frozen")
    if pin is not None and recorded != pin:
        raise ValueError(f"{path} is not the pinned probe set (its sha256 {recorded[:8]}... is not the pinned {pin[:8]}...): "
                         "a changed probe set needs a reviewed edit of PINNED_PROBES_SHA256 (or --allow-unpinned-probes to review one)")
    validate_probes(doc.get("probes"))
    if not ISO_DATE_RE.match(str(doc.get("as_of") or "")):
        raise ValueError(f"{path} has no ISO as_of date (the data date the judge is told)")
    return doc


# --- grading notes -----------------------------------------------------------------------------------------------------------

def notes_from_temporal(doc: dict, qid: str) -> str:
    """The gold-derived notes of a legacy question, exactly as ``build_temporal_questions.py --merge`` writes them into the
    benchmark (one source of truth: the acceptance test grades against the notes production scoring will use)."""
    notes = (doc.get("legacy_notes") or {}).get(qid)
    if not notes:
        raise ValueError(f"the temporal questions file has no legacy_notes for {qid}: rebuild it with build_temporal_questions.py")
    return notes


def notes_from_benchmark(benchmark: list[dict], qid: str) -> str:
    by_id = {b["id"]: b for b in benchmark}
    if qid not in by_id or not by_id[qid].get("judge_notes"):
        raise ValueError(f"the benchmark has no judge_notes for {qid}")
    return by_id[qid]["judge_notes"]


def load_temporal(args) -> dict:
    """The temporal questions file, refused unless it was built from the frozen source-text gold that is on disk."""
    if not args.temporal.exists():
        raise FileNotFoundError(f"{args.temporal} does not exist: run scripts/build_temporal_questions.py first (it needs the frozen gold)")
    doc = json.loads(args.temporal.read_text(encoding="utf-8"))
    if not args.gold.exists() or not gold.verify_frozen(args.gold):
        raise ValueError(f"{args.gold} is missing or not frozen: the temporal notes cannot be tied to a verified gold")
    frozen = json.loads(args.gold.read_text(encoding="utf-8"))["sha256"]
    if doc.get("gold_sha256") != frozen:
        raise ValueError(f"{args.temporal} was built from another gold (its gold_sha256 is not {frozen[:8]}...): rebuild it")
    return doc


def resolve_notes(probes: list[dict], *, temporal_doc: dict | None, benchmark: list[dict] | None) -> dict[str, str]:
    """Probe id -> grading notes. A ``temporal_legacy`` probe reads the temporal file's legacy_notes (or, when ``temporal_doc`` is
    None, the merged benchmark's notes of that id); a ``benchmark`` probe reads the benchmark's and its question must equal the
    benchmark's own, so a probe can never be graded against notes written for another question."""
    by_id = {b["id"]: b for b in benchmark or []}
    notes: dict[str, str] = {}
    for probe in probes:
        (source, qid), = probe["notes_from"].items()
        if source == "temporal_legacy" and temporal_doc is not None:
            notes[probe["id"]] = notes_from_temporal(temporal_doc, qid)
            continue
        notes[probe["id"]] = notes_from_benchmark(benchmark or [], qid)
        if source == "benchmark" and by_id[qid].get("q") != probe["question"]:
            raise ValueError(f"probe {probe['id']}: its question is not the benchmark's question for {qid}")
    return notes


def build_cases(probes: list[dict], notes: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """(benchmark items, run rows) keyed by PROBE id. The valid ids of a probe are the cited ids the service verified, so they are
    the ids the judge is told about; ``type`` "temporal" without an ``expect`` makes ``judge_open`` pay for every probe."""
    items = [{"id": p["id"], "type": "temporal", "q": p["question"], "judge_notes": notes[p["id"]]} for p in probes]
    rows = [{"id": p["id"], "answer": p["answer"], "valid_ids": list(p["valid_ids"]), "hallucinated": []} for p in probes]
    return items, rows


# --- cost, dry run ---------------------------------------------------------------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fail(message: object) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_UNUSABLE


def estimate_cost(prompt_lengths: list[int], votes: int) -> float:
    """Worst-case spend: the bake-off's conservative per-call bound, scaled up for a prompt longer than that bound assumes."""
    return sum(bakeoff.JUDGE_CALL_USD * max(1.0, n / BOUND_PROMPT_CHARS) * votes for n in prompt_lengths)


def _prompts(items: list[dict], rows: list[dict], as_of: str) -> list[str]:
    return [runner.render_judge_prompt(i, r["answer"], valid_ids=r["valid_ids"], as_of=as_of) for i, r in zip(items, rows)]


def _label(expected_correct: bool) -> str:
    return "correct" if expected_correct else "INCORRECT"


def _print_dry_run(probes: list[dict], prompts: list[str], as_of: str, votes: int, *, show_prompts: bool, pinned: bool) -> None:
    print("dry run: nothing is sent to any model (pass --go to run the paid acceptance test)")
    print(f"judge prompt {runner.JUDGE_PROMPT_VERSION} (sha256 {_sha(runner.JUDGE_PROMPT)[:12]}), as of {as_of}, "
          f"{len(probes)} probes, {votes} votes each" + ("" if pinned else " [probes NOT pinned]"))
    for probe, prompt in zip(probes, prompts):
        print(f"  {probe['id']:34} expected {_label(probe['expected_correct']):9} {len(prompt):6} characters")
        if show_prompts:
            print(f"\n===== {probe['id']} =====\n{prompt}\n")
    print(f"\n{len(probes) * votes} judge calls, worst case ${estimate_cost([len(p) for p in prompts], votes):.2f} "
          f"(${bakeoff.JUDGE_CALL_USD:.2f} per call is the bake-off's conservative bound, scaled for longer prompts)")


# --- verdict -----------------------------------------------------------------------------------------------------------------

def evaluate(probes: list[dict], judged: dict, votes: int) -> tuple[dict, list[str], list[str]]:
    """(per-probe results, leniency failures, strictness failures). The majority rule is ``judge_open``'s (more than half of the
    votes). A probe with an errored vote is ``errored``: it is in neither failure list (the whole run is INCONCLUSIVE)."""
    results, lenient, strict = {}, [], []
    for probe in probes:
        pid = probe["id"]
        correct_votes = judged["votes"][pid]
        majority_correct = correct_votes * 2 > votes
        if pid in judged["errors"]:
            outcome = "errored"
        elif probe["expected_correct"] == majority_correct:
            outcome = "ok"
        elif majority_correct:
            outcome = "leniency_failure"
            lenient.append(pid)
        else:
            outcome = "strictness_failure"
            strict.append(pid)
        results[pid] = {"kind": probe.get("kind"), "expected_correct": probe["expected_correct"], "votes_correct": correct_votes,
                        "majority_correct": majority_correct, "outcome": outcome, "verdicts": judged["details"][pid]}
    return results, lenient, strict


def _verdict(judged: dict, lenient: list[str], strict: list[str]) -> tuple[str, int]:
    if judged["errors"]:
        return "INCONCLUSIVE", EXIT_INCONCLUSIVE
    return ("FAIL", EXIT_FAIL) if lenient or strict else ("PASS", EXIT_PASS)


def _first_reason(entries: list[dict], want_correct: bool) -> str:
    for entry in entries:
        if "error" not in entry and entry["correct"] is want_correct:
            return f"{entry['reason']} | unsupported: {entry['unsupported_claims']}"
    return "(no vote)"


def _print_failures(title: str, ids: list[str], results: dict, votes: int) -> None:
    if not ids:
        return
    print(f"\n{title}")
    for pid in ids:
        r = results[pid]
        print(f"  {pid}: {r['votes_correct']} of {votes} votes 'correct' (expected {_label(r['expected_correct'])})")
        print(f"     e.g. {_first_reason(r['verdicts'], r['majority_correct'])}")


def _print_result(verdict: str, results: dict, lenient: list[str], strict: list[str], judged: dict, votes: int, pinned: bool) -> None:
    _print_failures("LENIENCY FAILURES (a wrong answer graded correct: the instrument is too lenient):", lenient, results, votes)
    _print_failures("STRICTNESS FAILURES (a consistent answer graded incorrect: the instrument is too strict):", strict, results, votes)
    for pid, err_votes in judged["errors"].items():
        errors = [v["error"] for v in results[pid]["verdicts"] if "error" in v]
        print(f"\n{pid}: {err_votes} of {votes} votes errored: {errors[0] if errors else '?'}")
    print("\nper probe (votes graded 'correct' / votes):")
    for pid, r in results.items():
        print(f"  {pid:34} expected {_label(r['expected_correct']):9} {r['votes_correct']}/{votes}  {r['outcome']}")
    meaning = {"PASS": "every wrong answer was graded incorrect and every consistent answer correct",
               "FAIL": "the judge grades a wrong answer correct and/or a consistent answer incorrect: the instrument is not accepted",
               "INCONCLUSIVE": "a judge call errored; rerun (an error is not a graded verdict)"}[verdict]
    print(f"\nacceptance: {verdict}: {meaning}" + ("" if pinned else " [probes NOT pinned: this is not an acceptance]"))


def _make_report(args, doc: dict, pinned: bool, as_of: str, model: str, items: list[dict], prompts: list[str], judged: dict,
                 results: dict, lenient: list[str], strict: list[str], verdict: str) -> dict:
    return {"verdict": verdict, "model": model, "votes": args.votes, "as_of": as_of,
            "judge_prompt_version": runner.JUDGE_PROMPT_VERSION, "prompt_sha256": _sha(runner.JUDGE_PROMPT),
            "probes_file": str(args.probes), "probes_sha256": doc["sha256"], "probes_pinned": pinned, "notes_source": args.notes,
            "notes_sha256": {i["id"]: _sha(i["judge_notes"]) for i in items}, "n_probes": len(items),
            "leniency_failures": lenient, "strictness_failures": strict, "errors": judged["errors"], "results": results,
            "estimated_cost_usd": round(estimate_cost([len(p) for p in prompts], args.votes), 4)}


# --- main --------------------------------------------------------------------------------------------------------------------

def _parse(argv: list[str] | None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--go", action="store_true", help="PAID: run the judge (every probe x votes)")
    mode.add_argument("--dry-run", action="store_true", help="print the prompt lengths and the cost estimate (the default)")
    ap.add_argument("--probes", type=Path, default=DEFAULT_PROBES)
    ap.add_argument("--allow-unpinned-probes", action="store_true", help="skip the PINNED_PROBES_SHA256 check (a probe set under review)")
    ap.add_argument("--show-prompts", action="store_true", help="dry run: also print every rendered prompt")
    ap.add_argument("--temporal", type=Path, default=DEFAULT_TEMPORAL)
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--notes", choices=("temporal", "benchmark"), default="temporal", help="where the LEGACY probes' notes come from")
    ap.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    ap.add_argument("--as-of", default=None, help="data date told to the judge (default: the probes file's as_of)")
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--votes", type=int, default=VOTES)
    ap.add_argument("--judge-model", default=None, help="default: settings.llm_model")
    return ap.parse_args(argv)


def _load_cases(args) -> tuple[dict, list[dict], list[dict], str]:
    """(probes document, items, rows, as_of); raises OSError/ValueError/KeyError on unusable input."""
    if args.votes < 1:
        raise ValueError("--votes must be at least 1")
    doc = load_probes(args.probes, pin=None if args.allow_unpinned_probes else PINNED_PROBES_SHA256)
    probes = doc["probes"]
    as_of = args.as_of or doc["as_of"]
    if not ISO_DATE_RE.match(as_of):
        raise ValueError(f"--as-of must be an ISO date, not {as_of!r}")
    legacy = [p for p in probes if "temporal_legacy" in p["notes_from"]]
    temporal = load_temporal(args) if legacy and args.notes == "temporal" else None
    needs_benchmark = any("benchmark" in p["notes_from"] for p in probes) or (legacy and args.notes == "benchmark")
    benchmark = json.loads(args.benchmark.read_text(encoding="utf-8")) if needs_benchmark else None
    items, rows = build_cases(probes, resolve_notes(probes, temporal_doc=temporal, benchmark=benchmark))
    return doc, items, rows, as_of


def main(argv: list[str] | None = None, *, judge=None) -> int:
    args = _parse(argv)
    try:
        doc, items, rows, as_of = _load_cases(args)
    except (OSError, ValueError, KeyError) as err:
        return _fail(err.args[0] if err.args else err)
    probes, pinned = doc["probes"], doc["sha256"] == PINNED_PROBES_SHA256      # pinned = it IS the accepted set, flag or not
    prompts = _prompts(items, rows, as_of)
    if not args.go:
        _print_dry_run(probes, prompts, as_of, args.votes, show_prompts=args.show_prompts, pinned=pinned)
        return EXIT_PASS
    if judge is None:
        from semigraph.llm import llm_json as judge     # only on --go: the paid path
    from semigraph.config import get_settings

    model = args.judge_model or get_settings().llm_model
    judged = bakeoff.judge_open(rows, items, judge, votes=args.votes, model=model, as_of=as_of, detail=True)
    results, lenient, strict = evaluate(probes, judged, args.votes)
    verdict, code = _verdict(judged, lenient, strict)
    report = _make_report(args, doc, pinned, as_of, model, items, prompts, judged, results, lenient, strict, verdict)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_result(verdict, results, lenient, strict, judged, args.votes, pinned)
    print(f"report -> {args.report}")
    return code


if __name__ == "__main__":
    sys.exit(main())

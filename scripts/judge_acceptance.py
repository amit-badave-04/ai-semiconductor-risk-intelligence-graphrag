"""INSTRUMENT ACCEPTANCE TEST for the correctness judge (docs/v2/M1B_PLAN.md section E).

The earlier judge, calibrated by labellers who saw only the retrieved context, graded the served T1 and T3 example answers
"correct". Both are WRONG (T1 says several risk factors were dropped although they are still in the filing; T3 uses pseudo-citations,
mislabels a fiscal year and repeats the false drops). The new judge is accepted only if it grades BOTH incorrect.

    PYTHONPATH=src python scripts/judge_acceptance.py               # DRY RUN (default): prints the prompts and the cost, calls nothing
    PYTHONPATH=src python scripts/judge_acceptance.py --go          # PAID (about $0.06-0.09 worst case): 2 answers x 3 votes, majority

The paid run goes through ``eval/bakeoff.py::judge_open``, the very function the benchmark scoring uses, so it tests the production
path and not a second loop. Grading notes come from the ``legacy_notes`` of ``artifacts/gold/temporal_questions.json`` (built ONLY
from the frozen source-text gold by scripts/build_temporal_questions.py): the very text ``--merge`` writes into the benchmark's T1
and T3, so the acceptance test grades against what production scoring will use. The temporal file must carry the sha256 of the
frozen gold on disk, else the script refuses. ``--notes benchmark`` reads the merged benchmark's T1/T3 notes instead (equal, once
merged). A PASS proves the removal rule works on T1/T3; it does not prove figure checking (the notes carry no financial figures).

Exit codes: 0 PASS (both graded incorrect, every vote answered); 1 FAIL (a wrong answer was graded correct: the instrument is not
accepted); 2 unusable input; 3 INCONCLUSIVE (a judge call errored: an errored vote counts as "not correct" inside ``judge_open``,
which for this test, where "incorrect" is the good outcome, would be a false PASS, so any error blocks the verdict).
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

DEFAULT_EXAMPLES = Path("src/semigraph/artifacts/examples.json")
DEFAULT_TEMPORAL = Path("artifacts/gold/temporal_questions.json")
DEFAULT_GOLD = Path("artifacts/gold/risk_items_gold.json")
DEFAULT_BENCHMARK = Path("src/semigraph/artifacts/benchmark.json")
DEFAULT_REPORT = Path("artifacts/judge_acceptance.json")
TARGETS = ("T1", "T3")                      # the served wrong answers
VOTES = 3
SNAPSHOT_RE = re.compile(r"^snap-(\d{4})(\d{2})(\d{2})-[0-9a-f]{10}$")
EXIT_PASS, EXIT_FAIL, EXIT_UNUSABLE, EXIT_INCONCLUSIVE = 0, 1, 2, 3


def snapshot_as_of(snapshot_id: str) -> str:
    m = SNAPSHOT_RE.match(snapshot_id or "")
    if not m:
        raise ValueError(f"not a snapshot id (snap-YYYYMMDD-<10 hex>): {snapshot_id!r}")
    return "-".join(m.groups())


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


def build_cases(examples: dict, notes: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """(benchmark items, run rows) for the served answers of ``notes``' ids. The cited ids of a served answer are the ones the
    service verified (``hallucinated`` is empty), so they are the valid ids the judge is told about."""
    by_id = {e["id"]: e for e in examples["examples"]}
    missing = [qid for qid in notes if qid not in by_id]
    if missing:
        raise ValueError(f"examples file has no served answer for {', '.join(missing)}")
    items = [{"id": qid, "type": "temporal", "q": by_id[qid]["question"], "judge_notes": notes[qid]} for qid in notes]
    rows = [{"id": qid, "answer": by_id[qid]["answer"], "valid_ids": list(by_id[qid]["citations"]),
             "hallucinated": list(by_id[qid].get("hallucinated") or [])} for qid in notes]
    return items, rows


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fail(message: object) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_UNUSABLE


def _load_notes(args) -> dict[str, str]:
    if args.notes == "benchmark":
        bench = json.loads(args.benchmark.read_text(encoding="utf-8"))
        return {qid: notes_from_benchmark(bench, qid) for qid in TARGETS}
    if not args.temporal.exists():
        raise FileNotFoundError(f"{args.temporal} does not exist: run scripts/build_temporal_questions.py first (it needs the frozen gold)")
    doc = json.loads(args.temporal.read_text(encoding="utf-8"))
    if not args.gold.exists() or not gold.verify_frozen(args.gold):
        raise ValueError(f"{args.gold} is missing or not frozen: the temporal notes cannot be tied to a verified gold")
    frozen = json.loads(args.gold.read_text(encoding="utf-8"))["sha256"]
    if doc.get("gold_sha256") != frozen:
        raise ValueError(f"{args.temporal} was built from another gold (its gold_sha256 is not {frozen[:8]}...): rebuild it")
    return {qid: notes_from_temporal(doc, qid) for qid in TARGETS}


BOUND_PROMPT_CHARS = 8000       # the bake-off's per-call bound (JUDGE_CALL_USD) assumes about 2k tokens in, 300 out


def estimate_cost(prompt_lengths: list[int], votes: int) -> float:
    """Worst-case spend: the bake-off's conservative per-call bound, scaled up for a prompt longer than that bound assumes."""
    return sum(bakeoff.JUDGE_CALL_USD * max(1.0, n / BOUND_PROMPT_CHARS) * votes for n in prompt_lengths)


def _print_dry_run(items: list[dict], rows: list[dict], as_of: str, votes: int) -> None:
    print("dry run: nothing is sent to any model (pass --go to run the paid acceptance test)")
    lengths = []
    for item, row in zip(items, rows):
        prompt = runner.render_judge_prompt(item, row["answer"], valid_ids=row["valid_ids"], as_of=as_of)
        lengths.append(len(prompt))
        print(f"\n===== {item['id']} ({len(prompt)} characters, {votes} votes) =====\n{prompt}")
    print(f"\n{len(items) * votes} judge calls, worst case ${estimate_cost(lengths, votes):.2f} "
          f"(${bakeoff.JUDGE_CALL_USD:.2f} per call is the bake-off's conservative bound, scaled for longer prompts)")


def _verdict(judged: dict) -> tuple[str, int]:
    if judged["errors"]:
        return "INCONCLUSIVE", EXIT_INCONCLUSIVE
    return ("PASS", EXIT_PASS) if judged["open_correct"] == 0 else ("FAIL", EXIT_FAIL)


def _print_result(verdict: str, judged: dict) -> None:
    for qid, votes in judged["details"].items():
        print(f"{qid}: {judged['votes'][qid]} of {len(votes)} votes 'correct'"
              + (f", {judged['errors'][qid]} errored" if qid in judged["errors"] else ""))
        for v in votes:
            print("   " + (f"ERROR {v['error']}" if "error" in v else
                           f"{'correct' if v['correct'] else 'incorrect'}: {v['reason']} | unsupported: {v['unsupported_claims']}"))
    meaning = {"PASS": "the new judge grades both served wrong answers incorrect",
               "FAIL": "the new judge grades a served wrong answer CORRECT: the instrument is not accepted",
               "INCONCLUSIVE": "a judge call errored; rerun (an error is not a graded verdict)"}[verdict]
    print(f"\nacceptance: {verdict}: {meaning}")


def main(argv: list[str] | None = None, *, judge=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--go", action="store_true", help="PAID: run the judge (2 answers x votes)")
    mode.add_argument("--dry-run", action="store_true", help="print the prompts and the cost estimate (the default)")
    ap.add_argument("--examples", type=Path, default=DEFAULT_EXAMPLES)
    ap.add_argument("--temporal", type=Path, default=DEFAULT_TEMPORAL)
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--notes", choices=("temporal", "benchmark"), default="temporal")
    ap.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--votes", type=int, default=VOTES)
    ap.add_argument("--judge-model", default=None, help="default: settings.llm_model")
    args = ap.parse_args(argv)
    try:
        examples = json.loads(args.examples.read_text(encoding="utf-8"))
        as_of = snapshot_as_of(examples["snapshot_id"])
        notes = _load_notes(args)
        items, rows = build_cases(examples, notes)
    except (OSError, ValueError, KeyError) as err:
        return _fail(err.args[0] if err.args else err)
    if not args.go:
        _print_dry_run(items, rows, as_of, args.votes)
        return EXIT_PASS
    if judge is None:
        from semigraph.llm import llm_json as judge     # only on --go: the paid path
    from semigraph.config import get_settings

    model = args.judge_model or get_settings().llm_model
    judged = bakeoff.judge_open(rows, items, judge, votes=args.votes, model=model, as_of=as_of, detail=True)
    verdict, code = _verdict(judged)
    report = {"verdict": verdict, "model": model, "votes": args.votes, "as_of": as_of, "notes_source": args.notes,
              "examples_snapshot": examples["snapshot_id"], "prompt_sha256": _sha(runner.JUDGE_PROMPT),
              "notes_sha256": {i["id"]: _sha(i["judge_notes"]) for i in items}, "open_correct": judged["open_correct"],
              "errors": judged["errors"], "results": judged["details"],
              "estimated_cost_usd": round(estimate_cost([len(runner.render_judge_prompt(i, r["answer"], valid_ids=r["valid_ids"],
                                                                                        as_of=as_of))
                                                         for i, r in zip(items, rows)], args.votes), 4)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_result(verdict, judged)
    print(f"report -> {args.report}")
    return code


if __name__ == "__main__":
    sys.exit(main())

"""Regenerate src/semigraph/artifacts/examples.json from a benchmark run of ONE graph snapshot.

    PYTHONPATH=src python scripts/build_examples.py \\
        --runs data/processed/eval_runs.v2-baseline.jsonl --snapshot snap-20260924-7feaaf9bfe

The service seeds these answers into its cache as free example clicks, but only when

- the file's snapshot id equals the running graph's (an answer from another data snapshot could cite text that is no
  longer current),
- the file's ``template_fingerprint`` equals the running build's (the answer prompt and context headers the answers were
  written under; stamped here from the CURRENT code: regenerate after the last prompt change), and
- the example's stored ``checks`` are present and clean.

The checks are computed HERE with the same ``answer_checks`` the service runs on every answer, from the run's own
``context`` and ``valid_ids`` (every run row of ``semigraph eval`` carries both), and stored in every example record. An
example that fails any of them (an ungrounded or question-echoed figure, an unretrieved citation, a pseudo-citation, an
unsupported removal claim, an uncited answer that is not a refusal) is still WRITTEN, with its failing checks: the service
refuses to seed it, and this script lists what it will refuse (``--strict`` turns that list into a non-zero exit). A run
that cannot be checked at all (no context / valid_ids, or a legacy context template) stops the build: re-run `eval`.
No LLM call: the answers are the hybrid runs already paid for by `eval`.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from semigraph.retrieval.answerer import CITE_RE, sources_from_context, template_fingerprint
from semigraph.retrieval.context_layout import CONTEXT_HEADERS
from semigraph.retrieval.verify import answer_checks
from semigraph.serve.store import refusal_reason

SNAPSHOT_RE = re.compile(r"^snap-[0-9]{8}-[0-9a-f]{10}$")
EXAMPLES_PATH = Path(__file__).resolve().parents[1] / "src" / "semigraph" / "artifacts" / "examples.json"
BENCHMARK_PATH = EXAMPLES_PATH.with_name("benchmark.json")


def compute_checks(run: dict, question: str) -> dict:
    """The ``checks`` of one saved run, exactly as the service would report them for that answer."""
    context, valid_ids = run.get("context"), run.get("valid_ids")
    if not context or valid_ids is None:
        raise ValueError("the run has no context / valid_ids (re-run `semigraph eval`: its rows carry both)")
    if not all(header in context for header in CONTEXT_HEADERS):
        raise ValueError("the run's context was not built with the current context template (re-run `semigraph eval`)")
    text = run["answer"]
    checks = answer_checks(text, set(CITE_RE.findall(text)), set(valid_ids), context,
                           sources=sources_from_context(context), question=question)
    return checks.as_dict()


def refused_examples(doc: dict) -> list[tuple[str, str]]:
    """``(id, reason)`` of every example the service would refuse to seed (the one rule: ``store.refusal_reason``)."""
    return [(e["id"], reason) for e in doc["examples"] if (reason := refusal_reason(e))]


def deployed_checks(run: dict) -> dict:
    """The ``checks`` the service reported on the ``done`` event of a deployed-path run (``semigraph eval-deployed``): the
    very object the seeding rule reads, so nothing is recomputed. A run without them cannot be seeded."""
    checks = run.get("checks")
    if not checks:
        raise ValueError("the deployed run has no checks (re-run `semigraph eval-deployed` with the current code)")
    return dict(checks)


def judged_incorrect(report: dict) -> dict[str, str]:
    """id -> reason for every question a deployed-eval report graded incorrect by the correctness judge's majority vote: the
    example answers are demo material, so an answer the judge rejected is not pre-seeded (a click on it is answered live)."""
    votes = int(report.get("votes") or 3)
    return {qid: f"judged incorrect by the correctness judge: {n} of {votes} votes correct"
            for qid, n in report["judged"]["votes"].items() if n * 2 <= votes}


def build_examples(runs: list[dict], benchmark: list[dict], snapshot_id: str, *, source: str,
                   strict: bool = False, deployed: bool = False, exclude: dict[str, str] | None = None) -> dict:
    """One example per benchmark question, in benchmark order, from its hybrid run, each with its computed ``checks``.

    With ``deployed`` the runs are ``eval-deployed`` rows (router, cheap draft, verifier, escalation) and carry the
    service's own ``checks`` instead of a context to recompute them from.

    exclude (id -> reason) leaves those questions out and records them under excluded.

    An example whose checks fail is written all the same (seeding refuses it; see :func:`refused_examples`); with
    ``strict`` the build raises instead, listing every one."""
    if not SNAPSHOT_RE.match(snapshot_id):
        raise ValueError(f"not a snapshot id: {snapshot_id!r} (expected snap-YYYYMMDD-<10 hex>)")
    hybrid = {r["id"]: r for r in runs if deployed or r["system"] == "hybrid"}
    missing = [b["id"] for b in benchmark if b["id"] not in hybrid]
    if missing:
        raise ValueError(f"no {'deployed' if deployed else 'hybrid'} run for benchmark question(s): {', '.join(missing)}")
    exclude = exclude or {}
    unknown = sorted(set(exclude) - {b["id"] for b in benchmark})
    if unknown:
        raise ValueError(f"cannot exclude {', '.join(unknown)}: not in the benchmark")
    examples = []
    for b in benchmark:
        if b["id"] in exclude:
            continue
        r = hybrid[b["id"]]
        if not r["answer"].strip():
            raise ValueError(f"{b['id']}: empty answer")
        if r["hallucinated"]:
            raise ValueError(f"{b['id']}: hallucinated citations {r['hallucinated']}")
        try:
            checks = deployed_checks(r) if deployed else compute_checks(r, b["q"])
        except ValueError as e:
            raise ValueError(f"{b['id']}: {e}") from e
        examples.append({"id": b["id"], "type": b["type"], "question": b["q"], "answer": r["answer"],
                         "citations": sorted(r["cited"]), "hallucinated": [], "checks": checks})
    doc = {"source": source, "snapshot_id": snapshot_id, "template_fingerprint": template_fingerprint(),
           "examples": examples}
    if exclude:
        doc["excluded"] = [{"id": qid, "reason": reason} for qid, reason in sorted(exclude.items())]
    refused = refused_examples(doc)
    if strict and refused:
        raise ValueError("examples that fail their own checks cannot be served:\n  "
                         + "\n  ".join(f"{i}: {reason}" for i, reason in refused))
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, required=True, help="benchmark runs jsonl (from `semigraph eval --runs-file`)")
    ap.add_argument("--deployed", action="store_true",
                    help="--runs is an `eval-deployed` file: rows carry the service's own checks (no context needed)")
    ap.add_argument("--snapshot", required=True, help="snapshot id of the graph those runs were answered against")
    ap.add_argument("--source", default=None, help="provenance line stored in the file")
    ap.add_argument("--report", type=Path, default=None,
                    help="the deployed-eval report of those runs: questions its judge graded incorrect are not seeded")
    ap.add_argument("--out", type=Path, default=EXAMPLES_PATH)
    ap.add_argument("--strict", action="store_true",
                    help="write nothing and exit non-zero when any example would be refused at seeding")
    args = ap.parse_args()
    runs = [json.loads(line) for line in args.runs.read_text(encoding="utf-8").splitlines() if line.strip()]
    benchmark = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    default_source = (f"deployed-path benchmark run {args.runs.name} (router, cheap draft, verifier, escalation)" if args.deployed
                      else f"benchmark run {args.runs.name}, hybrid system, Claude Sonnet 5")
    source = args.source or default_source
    try:
        exclude = judged_incorrect(json.loads(args.report.read_text(encoding="utf-8"))) if args.report else None
        doc = build_examples(runs, benchmark, args.snapshot, source=source, strict=args.strict, deployed=args.deployed,
                             exclude=exclude)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for entry in doc.get("excluded", []):
        print(f"  not seeded: {entry['id']}: {entry['reason']}")
    print(f"wrote {len(doc['examples'])} examples for {args.snapshot} (template {doc['template_fingerprint']}) -> {args.out}")
    for example_id, reason in refused_examples(doc):
        print(f"  the service will REFUSE to seed {example_id}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

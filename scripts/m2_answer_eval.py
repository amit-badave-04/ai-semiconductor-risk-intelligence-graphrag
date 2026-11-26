"""M2 I6: the answer-level paid evaluation (docs/specs/m2-search.md section 7, "Answer-level decision rule"; OD15, R11).

Two arms on the deployed-eval harness (``bakeoff.run_deployed`` / ``score_deployed`` path: router, cheap draft, verifier,
escalation): ``off`` is today's retrieval, ``on`` is fused retrieval with the FROZEN settings
(artifacts/m2_bench/frozen_settings.json, registered hash). Questions come from the TEST split only: every headroom question
(baseline context lacks the gold) plus a fixed, deterministic control sample (baseline context has it), strata read from
artifacts/m2_bench/test_report.json.

Modes:
  --dry-run        prints question count, arms, answer-cost model and judge worst case. Spends nothing, opens no connection.
  paid             refuses without --max-usd AND --confirm-paid; checkpoints every answer to a runs file (JSONL), retries
                   transient errors, stops at the cap (answers + judge calls share the cap; the judge worst case is reserved).
                   Re-running with the same --runs-file resumes: bought answers and judge votes are never bought twice.

Scoring per question and arm: a mechanical needle check (types B and C with a short reference answer) counts as correct on a
hit; a miss falls through to the majority-of-3 judge (a needle miss is never a failure by itself, wording varies). Every other
question goes to the judge. Claim support and citation validity are reported per arm. The decision (pre-registered): headroom
wins > losses with at most 1 loss, and control at most 1 loss.

Deliberate deviation from the spec text (to be recorded in the spec or a decision note): the run loop and scoring here are this
script's own (``run_arms`` / ``score_arm``), not ``run_deployed`` / ``score_deployed``; they call the same retrieval, answerer
(router, draft, verifier, escalation) and judge functions, but leave out the deployed eval's route/escalation tallies.
OD15 ledger: a gitignored file (default data/processed/m2_ledger.jsonl) holds spend; the committed report carries none. A "failed
paid run" is one that ended with unresolved provider errors; two stop further paid runs. A run whose outcome rests on a provider
error, a judge error or an 'on' answer without the rerank stage reports ``incomplete`` and is resumed, never decided.
"""

import argparse
import math
import hashlib
import importlib.util
import json
import math
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BENCH_DIR = ROOT / "artifacts" / "m2_bench"
TEST_BENCH = BENCH_DIR / "m2_bench_test.json"
TEST_REPORT = BENCH_DIR / "test_report.json"
FROZEN_SETTINGS = BENCH_DIR / "frozen_settings.json"
DEPLOYED_COST_REPORT = ROOT / "artifacts" / "eval_report.m2a3-deployed.json"
OUT_REPORT = BENCH_DIR / "answer_eval_report.json"

ARMS = ("off", "on")
VOTES = 3
CONTROL_N = 15
CONTROL_SEED = "m2-i6-control-v1"          # recorded in the report; changing it changes the sample
MECHANICAL_TYPES = frozenset({"B", "C"})
MAX_NEEDLE_WORDS = 8                       # a longer reference answer is judged, not needle-matched
ANSWER_WORST_FACTOR = 5.0                  # worst case per answer = this x the deployed eval's average (escalation + longer context)
ANSWER_CALL_FLOOR_USD = 0.01               # used ONLY if the deployed cost report is missing; the dry run says so, a paid run refuses
COST_SOURCE_REPORT = "deployed eval report"
COST_SOURCE_FALLBACK = "fallback"
M2_CAP_USD = 15.0                          # OD15: the M2-wide cap, tracked in a gitignored ledger (never in the repo)
MAX_FAILED_RUNS = 2                        # OD15: stop after two failed paid runs (ended with unresolved transient errors)
MAX_TRANSIENT_ATTEMPTS = 3
BACKOFF_S = (0, 5, 15)
TRANSIENT_NAMES = frozenset({"APIConnectionError", "ServiceUnavailableError", "InternalServerError", "RateLimitError",
                             "Timeout", "APITimeoutError", "OverloadedError"})
CONFIRM_FLAG = "--confirm-paid"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- questions ------------------------------------------------------------------------------------------------

def control_sample(ids: Iterable[str], n: int, seed: str = CONTROL_SEED) -> list[str]:
    """``n`` ids chosen by SHA-256(seed:id) order (stable under input order and under adding unrelated ids), returned sorted."""
    ranked = sorted(set(ids), key=lambda i: hashlib.sha256(f"{seed}:{i}".encode()).hexdigest())
    return sorted(ranked[:n])


def pick_questions(bench: Mapping, strata: Mapping[str, str], *, control_n: int = CONTROL_N,
                   headroom_limit: int | None = None, seed: str = CONTROL_SEED) -> list[dict]:
    """The answer-level questions: every headroom question (or the first ``headroom_limit`` by id) and the control sample.
    Refuses anything that is not the test split, so a dev question can never be bought."""
    questions = bench["questions"]
    if bench.get("split") != "test" or any(q.get("split") != "test" for q in questions):
        raise ValueError("the answer-level run draws from the test split only (a dev question was found)")
    by_id = {q["id"]: q for q in questions}
    unknown = sorted(set(strata) - set(by_id))
    if unknown:
        raise ValueError(f"strata name questions that are not in the test bench: {unknown[:3]}")
    headroom = sorted(i for i, s in strata.items() if s == "headroom")
    if headroom_limit is not None:
        headroom = headroom[:headroom_limit]
    control = control_sample((i for i, s in strata.items() if s == "control"), control_n, seed)
    return [{**by_id[i], "stratum": "headroom" if i in set(headroom) else "control"} for i in sorted(headroom + control)]


_FIGURE_RE = re.compile(r"(?<![\d.,])\d[\d,]*(?:\.\d+)?\s*(?:%|million|billion|trillion)", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[\-‐-―]", " ", text.lower())


def _figures(text: str) -> set[str]:
    return {re.sub(r"[\s,]", "", m.group(0)) for m in _FIGURE_RE.finditer(_norm(text))}


def needle_hit(needles: Iterable[str], answer: str) -> bool:
    """True when a needle appears in the answer on word and number boundaries ("2.7 billion" is not in "$12.7 billion", "80%" is
    not in "180%"). When the needle states a figure (amount or percentage), the answer must state no OTHER figure of that kind: an
    answer listing several amounts or periods is left to the judge. A miss is never a failure by itself."""
    text = _norm(answer)
    for needle in needles:
        n = _norm(needle).strip()
        if not n:
            continue
        lead = r"(?<![\w.,$])" if n[0].isdigit() else r"(?<!\w)"
        if not re.search(lead + re.escape(n) + r"(?!\w)(?![.,]\d)", text):
            continue
        wanted = _figures(n)
        if wanted and not _figures(text) <= wanted:
            continue
        return True
    return False


def mechanical_expect(item: Mapping) -> dict | None:
    """A needle check for a type B or C question with a short reference answer (``any_of``), else None.
    A hit (see :func:`needle_hit`) is accepted as correct; a miss is never a failure by itself (it goes to the judge)."""
    answer = (item.get("answer") or "").strip().rstrip(".").strip()
    if item.get("type") not in MECHANICAL_TYPES or not answer or len(answer.split()) > MAX_NEEDLE_WORDS:
        return None
    return {"any_of": [answer]}


def judge_item(item: Mapping) -> dict:
    """The item in the shape ``render_judge_prompt`` reads: question and verified grading notes (reference answer + filing quote)."""
    quote = " | ".join(item.get("quotes") or [])
    return {"id": item["id"], "type": "open", "q": item["q"],
            "judge_notes": f"Reference answer: {item['answer']}. The filing says: {quote}"}


# --- cost model and dry run -----------------------------------------------------------------------------------

def per_answer_usd(path: Path = DEPLOYED_COST_REPORT) -> tuple[float, str]:
    """(average answer cost per question of the deployed eval, its source). Source is COST_SOURCE_FALLBACK when the report is missing."""
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
        return float(rep["total_cost_usd"]) / int(rep["n"]), COST_SOURCE_REPORT
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return ANSWER_CALL_FLOOR_USD, COST_SOURCE_FALLBACK


def estimate(n_questions: int, arms: Sequence[str], avg_answer_usd: float, *, votes: int = VOTES,
             source: str = COST_SOURCE_REPORT) -> dict:
    """Worst case before anything is bought. The judge worst case assumes every answer reaches the judge (no needle hit)."""
    from semigraph.eval import bakeoff as bo

    answers = n_questions * len(arms)
    judge_usd = answers * votes * bo.JUDGE_CALL_USD
    return {"questions": n_questions, "arms": list(arms), "answers": answers, "avg_answer_usd": avg_answer_usd, "cost_source": source,
            "answers_expected_usd": answers * avg_answer_usd,
            "answers_worst_case_usd": answers * avg_answer_usd * ANSWER_WORST_FACTOR,
            "judge_calls_worst_case": answers * votes, "judge_worst_case_usd": judge_usd,
            "answer_model_cap": "enforced on the recorded spend (answers + judge) at --max-usd",
            "total_expected_usd": answers * avg_answer_usd + judge_usd,
            "total_worst_case_usd": answers * avg_answer_usd * ANSWER_WORST_FACTOR + judge_usd}


def format_estimate(est: Mapping, questions: Sequence[Mapping]) -> str:
    strata = {s: sum(1 for q in questions if q["stratum"] == s) for s in ("headroom", "control")}
    return "\n".join([
        f"m2 answer-level evaluation (dry run, nothing spent): {est['questions']} questions "
        f"({strata['headroom']} headroom, {strata['control']} control), test split only",
        f"arms: {', '.join(est['arms'])} -> {est['answers']} answers (each through router, draft, verifier, escalation)",
        f"answers: expected ${est['answers_expected_usd']:.2f} (avg ${est['avg_answer_usd']:.4f} per answer from the deployed eval), "
        f"worst case ${est['answers_worst_case_usd']:.2f} ({ANSWER_WORST_FACTOR:g}x)",
        f"judge worst case: {est['judge_calls_worst_case']} calls x ${est['judge_worst_case_usd'] / max(est['judge_calls_worst_case'], 1):.2f}"
        f" = ${est['judge_worst_case_usd']:.2f} (needle hits on B/C skip the judge)",
        f"total: expected ${est['total_expected_usd']:.2f}, worst case ${est['total_worst_case_usd']:.2f}",
        f"cap: {est['answer_model_cap']}; M2 ledger cap ${M2_CAP_USD:.2f} (gitignored ledger file)",
    ] + ([f"WARNING: the deployed cost report is missing; the per-answer cost is the ${ANSWER_CALL_FLOOR_USD} {COST_SOURCE_FALLBACK} "
          f"and a paid run refuses until it exists"] if est.get("cost_source") == COST_SOURCE_FALLBACK else []))


def refusals(est: Mapping, *, max_usd: float | None, confirm_paid: bool) -> list[str]:
    """Why a paid run must not start (empty = may start). The judge worst case is reserved out of the cap, so the answers' share
    must cover their EXPECTED cost; the worst case is enforced by stopping at the cap, not by refusing."""
    out = []
    if est.get("cost_source") == COST_SOURCE_FALLBACK:
        out.append("the deployed cost report is missing, so the estimate is a fallback, not a measurement")
    if not confirm_paid:
        out.append(f"a paid run needs {CONFIRM_FLAG} (it buys model calls; see the estimate above)")
    if max_usd is None or not math.isfinite(max_usd) or max_usd <= 0:
        out.append("a paid run needs an explicit positive, finite --max-usd")
    elif max_usd - est["judge_worst_case_usd"] < est["answers_expected_usd"]:
        out.append(f"--max-usd ${max_usd:.2f} cannot cover the expected answers ${est['answers_expected_usd']:.2f} plus the "
                   f"judge reserve ${est['judge_worst_case_usd']:.2f}")
    return out


# --- running (checkpointed, capped, transient-retrying) -----------------------------------------------------------

AnswerOne = Callable[..., tuple]     # (arm, question) -> (events, context) or (events, context, extras dict merged into the row)


def _is_transient(row: Mapping) -> bool:
    return str(row.get("error", "")).split(":", 1)[0].strip() in TRANSIENT_NAMES


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def runs_spend(path: Path) -> float:
    """Recorded answer spend in a runs file, superseded (retried) rows included: every row was bought."""
    return sum(r.get("cost_usd") or 0.0 for r in _read_rows(path))


def _attempt(arm: str, q: dict, answer_one: AnswerOne, sleep, spent: float, cap: float, worst: float = 0.0) -> dict:
    """One question on one arm with up to ``MAX_TRANSIENT_ATTEMPTS`` tries; failed tries' spend is carried into the row.
    An attempt starts only if one worst-case answer still fits under the cap."""
    from semigraph.eval import bakeoff as bo
    from semigraph.eval.runner import AnswerBudgetExceeded

    carried, row, extras = 0.0, {}, {}
    for attempt in range(MAX_TRANSIENT_ATTEMPTS):
        if not (math.isfinite(spent + carried) and math.isfinite(cap)) or spent + carried + worst > cap or spent + carried >= cap:
            raise AnswerBudgetExceeded(
                f"spend ${spent + carried:.3f} plus one worst-case answer reaches the ${cap:.2f} cap before {arm} {q['id']}")
        sleep(BACKOFF_S[attempt])
        t0 = time.monotonic()
        result = answer_one(arm, q)
        events, context = result[0], result[1]
        extras = result[2] if len(result) > 2 else {}
        row = bo._deployed_row(q, events, round(time.monotonic() - t0, 3), context)   # noqa: SLF001 - the deployed row shape
        carried += row.get("cost_usd") or 0.0
        if not (row.get("error") and _is_transient(row)):
            break
    return {**row, **extras, "arm": arm, "stratum": q["stratum"], "cost_usd": carried}


def run_arms(questions: Sequence[dict], arms: Sequence[str], answer_one: AnswerOne, path: Path, *, max_usd: float,
             sleep=time.sleep, answer_worst_usd: float = 0.0, run_tag: Mapping | None = None) -> list[dict]:
    """Answer every (question, arm), question-major so a stop at the cap leaves mostly paired data. Each row is appended and
    flushed as it is bought; a (arm, id) already answered in ``path`` is never bought again, EXCEPT a row that ended in a transient
    error, which is retried (its earlier spend still counts). ``run_tag`` (frozen-settings hash, snapshot) is stored in every row and
    a file written under a different tag is refused. ``AnswerBudgetExceeded`` stops the run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old_rows = _read_rows(path)
    tag = dict(run_tag) if run_tag is not None else None
    if tag is not None and any(r.get("run_tag") != tag for r in old_rows):
        raise SystemExit(f"refusing: {path.name} holds rows from a different run (frozen settings or snapshot differ); "
                         "use a new --runs-file")
    done = {(r["arm"], r["id"]): r for r in old_rows}
    spent = sum(r.get("cost_usd") or 0.0 for r in old_rows)
    with path.open("a", encoding="utf-8") as sink:
        for q in questions:
            for arm in arms:
                prior = done.get((arm, q["id"]))
                if prior is not None and not (prior.get("error") and _is_transient(prior)):
                    continue
                row = _attempt(arm, q, answer_one, sleep, spent, max_usd, answer_worst_usd)
                if tag is not None:
                    row["run_tag"] = tag
                spent += row["cost_usd"]
                sink.write(json.dumps(row) + "\n")
                sink.flush()
                done[(arm, q["id"])] = row
    return [done[(a, q["id"])] for q in questions for a in arms if (a, q["id"]) in done]


# --- the M2 ledger (OD15): gitignored, never committed, never copied into the report ---------------------------------------

class Ledger:
    """Append-only JSONL of paid steps. Spend lives here and in the runs file only, outside every committed path."""

    def __init__(self, path: Path):
        self.path = path

    def _rows(self) -> list[dict]:
        return _read_rows(self.path)

    def spent(self) -> float:
        return sum(r.get("usd") or 0.0 for r in self._rows())

    def failed_runs(self) -> int:
        return sum(1 for r in self._rows() if r.get("failed"))

    def append(self, step: str, usd: float, *, failed: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"step": step, "usd": round(usd, 6), "failed": failed}) + "\n")


def ledger_gate(ledger: Ledger, *, max_usd: float, est: Mapping) -> tuple[float, list[str]]:
    """(effective cap for this run, reasons not to start). The run cap is the smaller of ``--max-usd`` and what is left of the M2
    cap; a step that cannot cover its expected cost plus the judge reserve inside that is not started; two failed paid runs stop it."""
    cap = min(max_usd, M2_CAP_USD - ledger.spent())
    problems = []
    if not math.isfinite(max_usd) or not math.isfinite(cap) or not math.isfinite(ledger.spent()):
        problems.append("--max-usd and the ledger spend must be finite numbers")
        return 0.0, problems
    if ledger.failed_runs() >= MAX_FAILED_RUNS:
        problems.append(f"{ledger.failed_runs()} failed paid runs are already in the ledger (limit {MAX_FAILED_RUNS})")
    if cap <= 0:
        problems.append("the M2 cap is spent")
    elif cap - est["judge_worst_case_usd"] < est["answers_expected_usd"]:
        problems.append("the remaining M2 cap cannot cover the expected answers plus the judge reserve")
    return cap, problems


def require_reranker(get_reranker: Callable, key: str):
    """The frozen reranker, loaded once; stops the run if it does not load (the retriever would silently use the fused order)."""
    reranker = get_reranker(key)
    if reranker is None:
        raise SystemExit("stopped before spending: the frozen reranker did not load, the 'on' arm would run without it")
    return reranker


def check_snapshot(actual: str, expected: str) -> None:
    """The strata were classified on one graph snapshot; any other graph makes headroom and control meaningless."""
    if not actual or actual != expected:
        raise SystemExit(f"stopped before spending: graph snapshot {actual or '(none)'} differs from the strata's snapshot {expected}")


# --- scoring ----------------------------------------------------------------------------------------------------

def score_arm(rows: Sequence[dict], items: Mapping[str, dict], judge, *, votes: int = VOTES, as_of: str | None = None,
              model: str | None = None) -> dict:
    """Per-question correctness for one arm (``correct`` id -> bool) with claim support, citation validity and cost.
    Error rows are incorrect and never judged; a needle hit is correct without the judge; the rest take the majority vote."""
    from semigraph.eval import bakeoff as bo
    from semigraph.eval.support import claim_support_rate

    correct, needle, to_judge = {}, [], []
    for r in rows:
        expect = mechanical_expect(items[r["id"]])
        if r.get("error") or not r.get("answer"):
            correct[r["id"]] = False
        elif expect and needle_hit(expect["any_of"], r["answer"]):
            correct[r["id"]] = True
            needle.append(r["id"])
        else:
            to_judge.append(r)
    judged = bo.judge_open(to_judge, [judge_item(items[r["id"]]) for r in to_judge], judge, votes=votes, model=model,
                           as_of=as_of, detail=True)
    for r in to_judge:
        correct[r["id"]] = judged["votes"][r["id"]] * 2 > votes
    n = len(rows) or 1
    return {"n": len(rows), "correct": correct, "n_correct": sum(correct.values()), "needle_hits": needle,
            "judged": judged, "judge_errors": judged["errors"],
            "transient_errors": [r["id"] for r in rows if r.get("error") and _is_transient(r)],
            "rerank_deviations": [r["id"] for r in rows if r.get("rerank_ok") is False],
            "citation_validity": sum(1 for r in rows if not r.get("error") and not r["hallucinated"]) / n,
            "claim_support": claim_support_rate(list(rows)), "errors": [r["id"] for r in rows if r.get("error")],
            "_answers_usd": sum(r.get("cost_usd") or 0.0 for r in rows), "checks_failed": bo.failed_checks_of(list(rows))}


# --- the pre-registered decision ----------------------------------------------------------------------------------

def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided binomial sign test (ties dropped), as in M2-Bench."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def wilson(successes: int, n: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson interval for the share of discordant pairs that are wins (the stated interval; None with no discordant pair)."""
    if n == 0:
        return None
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def paired(off: Mapping[str, bool], on: Mapping[str, bool], ids: Iterable[str]) -> dict:
    ids = list(ids)
    wins = sum(1 for i in ids if on[i] and not off[i])
    losses = sum(1 for i in ids if off[i] and not on[i])
    both = sum(1 for i in ids if on[i] and off[i])
    return {"n": len(ids), "wins": wins, "losses": losses, "both_correct": both, "both_wrong": len(ids) - wins - losses - both,
            "sign_test_p": sign_test_p(wins, losses), "win_share_of_discordant_ci95": wilson(wins, wins + losses)}


def unreliable_ids(score: Mapping) -> list[str]:
    """Questions whose outcome may come from a provider failure or a deviation, not from the answer: a transient error row that
    survived the retries, a judge vote that raised, an 'on' row that ran without the rerank stage."""
    return sorted(set(score["transient_errors"]) | set(score["judge_errors"]) | set(score.get("rerank_deviations", ())))


def decide(off: Mapping[str, bool], on: Mapping[str, bool], strata: Mapping[str, str], *,
           unreliable: Iterable[str] = ()) -> dict:
    """Headroom wins > losses with at most 1 loss; control at most 1 loss. Incomplete pairs or unreliable outcomes make the decision ``incomplete``."""
    missing = sorted((set(strata) - set(off)) | (set(strata) - set(on)))
    if missing:
        return {"decision": "incomplete", "missing_ids": missing,
                "reason": f"{len(missing)} question(s) lack an answer on one arm; the rule is not applied"}
    bad = sorted(set(unreliable))
    if bad:
        return {"decision": "incomplete", "unreliable_ids": bad,
                "reason": f"{len(bad)} question(s) hit a provider error, a judge error or a missing rerank stage; re-run to resume, "
                          "the rule is not applied"}
    head = paired(off, on, (i for i, s in strata.items() if s == "headroom"))
    ctrl = paired(off, on, (i for i, s in strata.items() if s == "control"))
    head_ok = head["wins"] > head["losses"] and head["losses"] <= 1
    ctrl_ok = ctrl["losses"] <= 1
    verdict = ("answer value: flag on may be recommended (with the retrieval primary and no-loss gates)" if head_ok and ctrl_ok
               else "retrieval gain, no answer value: code stays merged with the flag off")
    return {"decision": "pass" if head_ok and ctrl_ok else "fail", "headroom": {**head, "passes": head_ok},
            "control": {**ctrl, "passes": ctrl_ok}, "verdict": verdict,
            "note": "the headroom stratum is small: read the interval, not a bare pass or fail"}


# --- judge verdict cache (a re-run never buys the same vote twice) --------------------------------------------------

def caching_judge(path: Path, judge, cost_sink: list | None = None):
    """Wrap ``judge(prompt, model_cls, **kw)`` so the n-th call for a prompt is stored in ``path`` and replayed on resume."""
    cache = {}
    for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else []):
        row = json.loads(line)
        cache[(row["key"], row["n"])] = row["verdict"]
    seen: dict[str, int] = {}

    def wrapped(prompt, model_cls, **kw):
        key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        n = seen[key] = seen.get(key, -1) + 1
        if (key, n) in cache:
            return model_cls(**cache[(key, n)])
        verdict = judge(prompt, model_cls, **kw)
        if cost_sink is not None:
            cost_sink.append(1)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, "n": n, "verdict": verdict.model_dump()}) + "\n")
        return verdict

    return wrapped


# --- live wiring (paid mode only; never reached by --dry-run or the tests) ------------------------------------------------

def live_answer_one(settings) -> "tuple[AnswerOne, Callable[[], None], dict]":
    """The deployed path on both arms: ``off`` = ``hybrid_retrieve`` as ``run_deployed`` calls it, ``on`` = the same call with the
    frozen FusionConfig, the frozen reranker (loaded once, refused if it does not load) and a keyword index built from the graph.
    Everything is checked before the first paid call: frozen hash, reranker file and load, graph snapshot against the strata.
    Returns (answer_one, close, run_tag)."""
    from semigraph.embeddings import Embedder
    from semigraph.graph import client
    _load_script("m2_bench").require_local_graph(settings.neo4j_uri)       # S-06: never run paid answers against a remote graph
    from semigraph.retrieval.answerer import build_blocks, stream_answer_for_context
    from semigraph.retrieval.keyword import build_index, load_rows
    from semigraph.retrieval.rerank import get_reranker
    from semigraph.retrieval.retriever import hybrid_retrieve
    from semigraph.serve import store

    bench = _load_script("m2_bench")
    frozen_sha = bench.require_frozen_settings(FROZEN_SETTINGS)
    frozen = json.loads(FROZEN_SETTINGS.read_text(encoding="utf-8"))
    bench.check_frozen_reranker(frozen)
    cfg = bench.frozen_candidate(FROZEN_SETTINGS)
    reranker = require_reranker(get_reranker, cfg.rerank)
    driver, embedder = client.get_driver(settings), Embedder()
    try:
        snapshot = (store.current_snapshot(driver) or {}).get("id", "")
        check_snapshot(snapshot, json.loads(TEST_REPORT.read_text(encoding="utf-8"))["snapshot"])
        index = build_index(load_rows(driver), with_context=cfg.keyword_context, snapshot_id=snapshot)
        if index.size == 0:
            raise SystemExit("stopped before spending: the keyword index is empty")
        if not settings.escalation_model:
            raise SystemExit("no escalation model: set ESCALATION_MODEL (the run must match the deployed configuration)")
    except BaseException:
        driver.close()
        raise
    sys.stderr.write(f"frozen settings {frozen_sha[:16]}, keyword index {index.size} spans, snapshot {snapshot}\n")
    escalation = settings.escalation_model

    def answer_one(arm: str, q: dict):
        on = arm == "on"
        extra = {"fusion": cfg, "keyword_index": index, "reranker": reranker} if on else {}
        r = hybrid_retrieve(q["q"], driver, embedder, k_chunks=8, hops=2, **extra)
        events = list(stream_answer_for_context(q["q"], r, "hybrid", model=settings.answer_model, escalation_model=escalation,
                                                max_tokens=2400, timeout=90))
        return events, build_blocks(r)[1], ({"rerank_ok": "rerank_s" in r} if on else {})

    return answer_one, driver.close, {"frozen_sha256": frozen_sha, "snapshot": snapshot}


def build_report(questions: Sequence[dict], arm_scores: Mapping[str, dict], decision: dict, *, runs_file: Path,
                 headroom_limit: int | None) -> dict:
    """The committed report. It carries NO spend: no estimate, no cost, no dollar field (spec lines 122 and 301); spend lives in the
    gitignored runs file and ledger."""
    arms = {arm: {k: v for k, v in score.items() if not k.startswith("_")} for arm, score in arm_scores.items()}
    return {"spec": "docs/specs/m2-search.md section 7 (answer-level decision rule)", "split": "test",
            "frozen_settings_sha256": _load_script("m2_bench").file_sha256(FROZEN_SETTINGS),
            "questions": [{"id": q["id"], "type": q["type"], "stratum": q["stratum"]} for q in questions],
            "control_sample": {"seed": CONTROL_SEED, "n": CONTROL_N, "ids": [q["id"] for q in questions if q["stratum"] == "control"]},
            "headroom_limit": headroom_limit, "runs_file": runs_file.name, "arms": arms, "decision": decision}


# --- command line -------------------------------------------------------------------------------------------------

def _finite_usd(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite number")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--dry-run", action="store_true", help="print the exact counts and the worst case; spend nothing")
    p.add_argument("--max-usd", type=_finite_usd, default=None, help="hard cap on recorded spend (answers + judge); required for a paid run")
    p.add_argument("--confirm-paid", action="store_true", help="explicit authorisation to buy model calls")
    p.add_argument("--runs-file", default=None, help="JSONL checkpoint (default: data/processed/m2_answer_eval_runs.jsonl)")
    p.add_argument("--ledger-file", default=None, help="gitignored M2 spend ledger (default: data/processed/m2_ledger.jsonl)")
    p.add_argument("--headroom-limit", type=int, default=None, help="use only the first N headroom ids (default: all)")
    p.add_argument("--control-n", type=int, default=CONTROL_N)
    return p.parse_args(argv)


def load_plan(headroom_limit: int | None, control_n: int) -> tuple[list[dict], dict]:
    bench = json.loads(TEST_BENCH.read_text(encoding="utf-8"))
    strata = json.loads(TEST_REPORT.read_text(encoding="utf-8"))["strata"]
    _load_script("m2_bench").require_frozen_settings(FROZEN_SETTINGS)
    questions = pick_questions(bench, strata, control_n=control_n, headroom_limit=headroom_limit)
    usd, source = per_answer_usd()
    return questions, estimate(len(questions), ARMS, usd, source=source)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    questions, est = load_plan(args.headroom_limit, args.control_n)
    print(format_estimate(est, questions))
    if args.dry_run:
        return 0
    problems = refusals(est, max_usd=args.max_usd, confirm_paid=args.confirm_paid)
    if problems:
        print("refusing: " + "; ".join(problems) + ". Nothing was spent.", file=sys.stderr)
        return 2
    return run_paid(args, questions, est)


def run_paid(args: argparse.Namespace, questions: list[dict], est: Mapping) -> int:
    from semigraph.config import get_settings
    from semigraph.eval import bakeoff as bo
    from semigraph.eval.runner import AnswerBudgetExceeded, data_as_of
    from semigraph.llm import llm_json

    settings = get_settings()
    runs = Path(args.runs_file) if args.runs_file else settings.processed_dir / "m2_answer_eval_runs.jsonl"
    ledger = Ledger(Path(args.ledger_file) if args.ledger_file else settings.processed_dir / "m2_ledger.jsonl")
    cap, problems = ledger_gate(ledger, max_usd=args.max_usd, est=est)
    if problems:
        print("refusing: " + "; ".join(problems) + ". Nothing was spent.", file=sys.stderr)
        return 2
    answer_one, close, tag = live_answer_one(settings)
    before, judge_calls, failed = runs_spend(runs), [], False
    try:
        rows = run_arms(questions, ARMS, answer_one, runs, max_usd=cap - est["judge_worst_case_usd"], run_tag=tag,
                        answer_worst_usd=est["avg_answer_usd"] * ANSWER_WORST_FACTOR)
        items = {q["id"]: q for q in questions}
        judge = caching_judge(runs.with_suffix(".judge.jsonl"), llm_json, judge_calls)
        scores = {arm: score_arm([r for r in rows if r["arm"] == arm], items, judge, as_of=data_as_of(settings)) for arm in ARMS}
        bad = sorted(set(unreliable_ids(scores["off"])) | set(unreliable_ids(scores["on"])))
        failed = bool(bad)
    except AnswerBudgetExceeded as e:
        print(f"Stopped: {e}. Nothing further was spent; answers so far are checkpointed in {runs}.", file=sys.stderr)
        return 5
    finally:
        close()
        ledger.append("i6-answer-level", runs_spend(runs) - before + len(judge_calls) * bo.JUDGE_CALL_USD, failed=failed)
    strata = {q["id"]: q["stratum"] for q in questions}
    decision = decide(scores["off"]["correct"], scores["on"]["correct"], strata, unreliable=bad)
    OUT_REPORT.write_text(json.dumps(build_report(questions, scores, decision, runs_file=runs, headroom_limit=args.headroom_limit),
                                     indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(decision, indent=1))
    print(f"report -> {OUT_REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

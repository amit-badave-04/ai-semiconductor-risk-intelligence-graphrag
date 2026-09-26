"""Model bake-off: can a cheaper model answer the benchmark from the SAME retrieved context as the baseline?

Every candidate receives the byte-identical prompt the baseline (Sonnet 5) saw — rebuilt from the saved
``context`` of its run — so a difference in the answer is a difference in the model, not in retrieval.
Order of work, cheapest first:

1. answers (paid, checkpointed, spend-capped) -> 2. mechanical scoring + the deterministic verifier (free)
-> 3. the paid correctness judge (majority of N votes) ONLY for candidates that cleared the free gates.

Gates (docs/v2/PLAN.md): 100% on the mechanical questions, citation validity 100%, escalation rate <= 15%,
no provider errors, and judged correctness on the open questions no worse than the baseline measured the same way.
"""

import hashlib
import json
import logging
import time
from pathlib import Path

from ..llm import TRANSIENT
from ..llm_shape import completion_params
from ..retrieval.answerer import (ANSWER_PROMPT, CITE_RE, CONTEXT_HEADERS, LEGACY_CONTEXT_HEADERS, NONE_BLOCK,
                                  ContextBlocks, answer_stream, render_prompt, sources_from_context, usage_cost)
from ..retrieval.verify import failed_check_names, verify_answer
from .expect import check_expectation
from .runner import (JUDGE_MAX_TOKENS, JUDGE_PROMPT, JUDGE_PROMPT_VERSION, NUM_PAT, REFUSAL_PAT,  # noqa: F401
                     AnswerBudgetExceeded, Correct, needs_judge, parse_numbers, render_judge_prompt)

logger = logging.getLogger("semigraph.bakeoff")

ANSWER_MAX_TOKENS = 2400          # what production allows (LLM_ANSWER_MAX_TOKENS on Fly); no regeneration
OUT_TOKENS_ASSUMED = 1300         # typical hybrid answer (v2 baseline: ~1.2k completion tokens), for estimates
JUDGE_CALL_USD = 0.01             # conservative upper bound per Sonnet correctness-judge call (~2k in, 300 out)

# --- prompt reconstruction -------------------------------------------------------------------------------

def _split_on(context: str, headers: tuple[str, ...]) -> tuple[str, ...] | None:
    """The blocks between ``headers`` (None if the context does not use this template); never guesses."""
    if not context.startswith(headers[0]):
        return None
    starts, cursor, blocks = [len(headers[0])], len(headers[0]), []
    for header in headers[1:]:
        at = context.find(header, cursor)
        if at < 0:
            return None
        blocks.append(context[starts[-1]:at])
        cursor = at + len(header)
        starts.append(cursor)
    blocks.append(context[cursor:])
    rebuilt = "".join(h + b for h, b in zip(headers, blocks, strict=True))
    return tuple(blocks) if rebuilt == context else None


def split_context(context: str) -> ContextBlocks:
    """Invert ``build_blocks``' full-context template into its six blocks; refuses anything it cannot reproduce.

    A context saved before M1b (five blocks, DROPPED RISK LINEAGES) is accepted too: its external and temporal blocks
    become ``(none)``. The old dropped-lineage text was never text-verified, so it is NOT re-rendered under the new
    "text-verified" heading."""
    current = _split_on(context, CONTEXT_HEADERS)
    if current is not None:
        return ContextBlocks(*current)
    legacy = _split_on(context, LEGACY_CONTEXT_HEADERS)
    if legacy is not None:
        edges, metrics, risks, _dropped, chunks = legacy
        return ContextBlocks(edges, NONE_BLOCK, metrics, risks, NONE_BLOCK, chunks)
    raise ValueError("context is not a current or a legacy answer context (headers missing or out of order)")


def build_prompt(question: str, context: str) -> str:
    return render_prompt(question, split_context(context))


# --- answering -------------------------------------------------------------------------------------------

def litellm_complete(model: str, prompt: str, max_tokens: int) -> dict:
    """One non-streaming completion with the provider's own call shape; transient errors get two short retries."""
    from litellm import completion

    last = None
    for wait in (0, 5, 15):
        time.sleep(wait)
        t0 = time.monotonic()
        try:
            resp = completion(model=model, messages=[{"role": "user", "content": prompt}],
                              **completion_params(model, max_tokens), num_retries=2, timeout=180)
        except TRANSIENT as e:
            last = e
            continue
        choice = resp.choices[0]
        u = resp.usage
        return {"text": choice.message.content or "", "finish_reason": choice.finish_reason,
                "usage": {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens},
                "latency_s": round(time.monotonic() - t0, 3)}
    raise last  # type: ignore[misc]


def probe_model(model: str, complete=litellm_complete) -> tuple[bool, str]:
    """One tiny call (well under a cent): does this model take the call shape and return text?"""
    try:
        out = complete(model, "Reply with exactly: OK", 300)
    except Exception as e:  # noqa: BLE001 — a failed probe removes the candidate, it must not abort the run
        return False, f"{type(e).__name__}: {str(e)[:160]}"
    return (bool(out["text"].strip()), f"{out['usage']['completion_tokens']} output tokens")


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def answer_candidates(base_rows: list[dict], models: list[str], complete, path: Path, *,
                      max_usd: float | None, price=usage_cost, max_tokens: int = ANSWER_MAX_TOKENS) -> list[dict]:
    """Answer every baseline question with every model, appending each row to ``path`` as it is bought.

    Resumable (a (model, id) already in ``path`` is not paid for again) and capped: ``AnswerBudgetExceeded`` is
    raised before the next paid call once the file's recorded spend has reached ``max_usd``. A provider error
    becomes a failed row (``error`` set, empty answer), never an exception."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_rows(path)
    done = {(r["model"], r["id"]): r for r in existing}
    spent = sum(r.get("cost_usd") or 0.0 for r in existing)
    with path.open("a", encoding="utf-8") as sink:
        for model in models:
            for base in base_rows:
                key = (model, base["id"])
                if key in done:
                    continue
                if max_usd is not None and spent >= max_usd:
                    raise AnswerBudgetExceeded(f"spend ${spent:.3f} reached the ${max_usd:.2f} cap before {model} {base['id']}")
                row = _answer_one(model, base, complete, price, max_tokens)
                spent += row["cost_usd"] or 0.0
                sink.write(json.dumps(row) + "\n")
                sink.flush()
                done[key] = row
    wanted = {(m, b["id"]) for m in models for b in base_rows}
    return [r for k, r in done.items() if k in wanted]


def _answer_one(model: str, base: dict, complete, price, max_tokens: int) -> dict:
    valid_ids = sorted(base["valid_ids"])
    row = {"model": model, "id": base["id"], "answer": "", "cited": [], "hallucinated": [], "valid_ids": valid_ids,
           "finish_reason": None, "usage": None, "cost_usd": 0.0, "latency_s": None}
    try:
        out = complete(model, build_prompt(base["q"], base["context"]), max_tokens)
    except Exception as e:  # noqa: BLE001
        logger.warning("%s %s failed: %s", model, base["id"], e)
        return {**row, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    cited = set(CITE_RE.findall(out["text"]))
    return {**row, "answer": out["text"], "cited": sorted(cited), "hallucinated": sorted(cited - set(valid_ids)),
            "finish_reason": out["finish_reason"], "usage": out["usage"], "latency_s": out["latency_s"],
            "cost_usd": price(out["usage"], model)}


# --- scoring ---------------------------------------------------------------------------------------------

def _mechanical(item: dict, answer: str) -> bool | None:
    """True/False for questions with a deterministic expectation; None for open questions (need the judge)."""
    if item["type"] == "refusal":
        return bool(REFUSAL_PAT.search(answer))
    return check_expectation(item["expect"], answer) if item.get("expect") else None


def score_mechanical(rows: list[dict], benchmark: list[dict], contexts: dict[str, str] | None = None) -> dict:
    """Free scoring of one model's rows: deterministic correctness, citation validity, verifier failures.

    ``contexts`` (question id -> the retrieved context the model saw) lets the verifier accept an uncited dollar
    figure that is verifiably in that context (XBRL metrics have no chunk id to cite)."""
    by_id = {b["id"]: b for b in benchmark}
    passed = failed = 0
    failed_ids, escalation_ids = [], []
    valid_cites = 0
    for r in rows:
        errored = bool(r.get("error"))
        verdict = None if errored else _mechanical(by_id[r["id"]], r["answer"])
        if errored and _mechanical_kind(by_id[r["id"]]):
            failed += 1
            failed_ids.append(r["id"])
        elif verdict is not None:
            passed += verdict
            failed += not verdict
            if not verdict:
                failed_ids.append(r["id"])
        if not errored and not r["hallucinated"]:
            valid_cites += 1
        context = (contexts or {}).get(r["id"])
        # production passes the same three extras (``answerer._draft_then_escalate`` -> ``verify_answer``; the ``checks`` of
        # the done event come from ``answerer.answer_checks``): sources bound percentages to cited text, and the question
        # identifies the figures only the asker stated (never grounded)
        reasons = ["error"] if errored else verify_answer(
            r["answer"], set(r["cited"]), set(r["valid_ids"]), r["finish_reason"], context=context,
            sources=sources_from_context(context) if context else None, question=by_id[r["id"]].get("q"))
        if reasons:
            escalation_ids.append(r["id"])
    n = len(rows) or 1
    costs = [r["cost_usd"] or 0.0 for r in rows]
    lat = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
    out_toks = [(r["usage"] or {}).get("completion_tokens", 0) for r in rows]
    return {"n": len(rows), "mechanical": {"passed": passed, "of": passed + failed}, "failed_ids": failed_ids,
            "citation_validity": valid_cites / n, "escalation_rate": len(escalation_ids) / n,
            "escalation_ids": escalation_ids, "errors": sum(1 for r in rows if r.get("error")),
            "truncated": sum(1 for r in rows if r.get("finish_reason") == "length"),
            "avg_cost_usd": sum(costs) / n, "avg_latency_s": (sum(lat) / len(lat)) if lat else None,
            "avg_completion_tokens": sum(out_toks) / n}


def _mechanical_kind(item: dict) -> bool:
    return item["type"] == "refusal" or bool(item.get("expect"))


def open_question_count(benchmark: list[dict]) -> int:
    """How many benchmark questions the correctness judge is paid for (open questions and misattribution probes)."""
    return sum(1 for b in benchmark if needs_judge(b))


def judge_open(rows: list[dict], benchmark: list[dict], judge, *, votes: int = 3, model: str | None = None,
               as_of: str | None = None, detail: bool = False) -> dict:
    """Correctness of the questions that need the judge (the OPEN questions and the misattribution probes; every other
    question is deterministic) by majority of ``votes`` judge calls, with the prompt of ``render_judge_prompt``.

    One vote is not trustworthy: the same answer flipped 1-of-3 in this project's own re-judging. A judge call that
    raises counts as a "not correct" vote AND is recorded in ``errors`` (id -> errored votes), so a caller that treats
    "incorrect" as the good outcome can tell a graded verdict from a provider failure. ``detail`` adds ``details`` (id -> one
    entry per vote: the verdict with its reason and unsupported claims, or the error). Provider-error rows are not judged."""
    by_id = {b["id"]: b for b in benchmark}
    tally, errors, details, correct, total = {}, {}, {}, 0, 0
    for r in rows:
        item = by_id[r["id"]]
        if not needs_judge(item):
            continue
        total += 1
        n_true, n_err, entries = 0, 0, []
        if not r.get("error"):
            prompt = render_judge_prompt(item, r["answer"], valid_ids=r.get("valid_ids") or (), as_of=as_of)
            for _ in range(votes):
                try:
                    verdict = judge(prompt, Correct, model=model, max_tokens=JUDGE_MAX_TOKENS)
                except Exception as e:  # noqa: BLE001
                    logger.warning("judge vote failed for %s: %s", r["id"], e)
                    n_err += 1
                    entries.append({"error": f"{type(e).__name__}: {str(e)[:160]}"})
                    continue
                n_true += bool(verdict.correct)
                entries.append({"correct": bool(verdict.correct), "reason": verdict.reason,
                                "unsupported_claims": list(verdict.unsupported_claims)})
        tally[r["id"]] = n_true
        if n_err:
            errors[r["id"]] = n_err
        details[r["id"]] = entries
        correct += n_true * 2 > votes
    out = {"open_correct": correct, "open_of": total, "votes": tally, "errors": errors}
    return {**out, "details": details} if detail else out


# --- estimate --------------------------------------------------------------------------------------------

def estimate(base_rows: list[dict], candidates: list[tuple[str, float, float]], *, votes: int, open_questions: int) -> dict:
    """Worst-case spend before anything is bought. ``candidates`` = (model, $ per M input, $ per M output)."""
    prompts = [(b.get("usage") or {}).get("prompt_tokens") or 0 for b in base_rows]
    avg_prompt = sum(prompts) / len(prompts)
    answers = sum(len(base_rows) * (avg_prompt * i / 1e6 + OUT_TOKENS_ASSUMED * o / 1e6) for _, i, o in candidates)
    judge_worst = len(candidates) * open_questions * votes * JUDGE_CALL_USD
    rejudge = open_questions * votes * JUDGE_CALL_USD  # the baseline measured the same way
    return {"candidates": len(candidates), "avg_prompt_tokens": round(avg_prompt), "answers_usd": answers,
            "judge_worst_case_usd": judge_worst, "baseline_rejudge_usd": rejudge,
            "total_worst_case_usd": answers + judge_worst + rejudge}


# --- gates and orchestration -----------------------------------------------------------------------------

MAX_ESCALATION_RATE = 0.15


def passes_free_gates(score: dict) -> list[str]:
    """The gates that cost nothing to check; returns what failed (empty list = all cleared)."""
    failed = []
    m = score["mechanical"]
    if m["passed"] != m["of"]:
        failed.append(f"mechanical {m['passed']}/{m['of']}")
    if score["citation_validity"] < 1.0:
        failed.append(f"citation validity {score['citation_validity']:.2f}")
    if score["escalation_rate"] > MAX_ESCALATION_RATE:
        failed.append(f"escalation rate {score['escalation_rate']:.2f} > {MAX_ESCALATION_RATE:.2f}")
    if score["errors"]:
        failed.append(f"{score['errors']} provider error(s)")
    return failed


def baseline_as_row(base: dict) -> dict:
    """A saved baseline run in the same shape as a candidate's answer row, so it is scored identically."""
    return {"model": "baseline", "id": base["id"], "answer": base["answer"], "cited": list(base["cited"]),
            "hallucinated": list(base["hallucinated"]), "valid_ids": base["valid_ids"], "finish_reason": "stop",
            "usage": base.get("usage"), "cost_usd": base.get("cost_usd"), "latency_s": base.get("latency_s")}


def judge_stamp(benchmark: list[dict], as_of: str | None, judge_model: str | None) -> str:
    """sha256 over everything that decides a verdict besides the answer: the judge prompt template, the token budget, the as-of
    date, the judging model and the grading notes of every question the judge is paid for."""
    notes = {b["id"]: b.get("judge_notes") or "" for b in benchmark if needs_judge(b)}
    payload = {"prompt": JUDGE_PROMPT, "max_tokens": JUDGE_MAX_TOKENS, "as_of": as_of, "model": judge_model, "notes": notes}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _reusable(previous: dict | None, votes: int, entry: dict | None, stamp: str) -> dict | None:
    """A judgement from an earlier report, valid only for the same vote count AND the same judge stamp (the answers never
    change, the instrument does: a report without a stamp, such as one made by the pre-M1b judge, is never reused)."""
    if previous and previous.get("votes") == votes and previous.get("judge_stamp") == stamp and entry and entry.get("judged"):
        return entry["judged"]
    return None


def run_bakeoff(base_rows: list[dict], benchmark: list[dict], models: list[str], *, complete, judge, runs_path: Path,
                max_usd: float | None, votes: int = 3, price=usage_cost, judge_model: str | None = None,
                previous: dict | None = None, as_of: str | None = None) -> dict:
    """Answer, score for free, then judge only what cleared the free gates. The baseline is judged the same way.

    ``previous`` is an earlier report whose judgements are reused (same vote count) instead of bought again;
    ``as_of`` is the data date the correctness judge is told (None = "not stated")."""
    rows = answer_candidates(base_rows, models, complete, runs_path, max_usd=max_usd, price=price)
    contexts = {b["id"]: b["context"] for b in base_rows}
    baseline_rows = [baseline_as_row(b) for b in base_rows]
    baseline = score_mechanical(baseline_rows, benchmark, contexts)
    baseline["gates_failed"] = passes_free_gates(baseline)
    stamp = judge_stamp(benchmark, as_of, judge_model)
    baseline["judged"] = (_reusable(previous, votes, (previous or {}).get("baseline"), stamp)
                          or judge_open(baseline_rows, benchmark, judge, votes=votes, model=judge_model, as_of=as_of))
    report = {"votes": votes, "judge_stamp": stamp, "baseline": baseline, "models": {}}
    for model in models:
        mine = [r for r in rows if r["model"] == model]
        score = score_mechanical(mine, benchmark, contexts)
        score["gates_failed"] = passes_free_gates(score)
        score["judged"] = None if score["gates_failed"] else (
            _reusable(previous, votes, ((previous or {}).get("models") or {}).get(model), stamp)
            or judge_open(mine, benchmark, judge, votes=votes, model=judge_model, as_of=as_of))
        score["clears_all_gates"] = bool(score["judged"]) and score["judged"]["open_correct"] >= baseline["judged"]["open_correct"]
        report["models"][model] = score
    return report


def model_prices(model: str) -> tuple[float, float]:
    """($ per million input tokens, $ per million output tokens) from LiteLLM's cost map; raises if unknown."""
    import litellm

    i = litellm.cost_per_token(model=model, prompt_tokens=1_000_000, completion_tokens=0)[0]
    o = litellm.cost_per_token(model=model, prompt_tokens=0, completion_tokens=1_000_000)[1]
    return i, o


# --- the deployed configuration, end to end ---------------------------------------------------------------

def run_deployed(benchmark: list[dict], driver, embedder, path: Path, *, model: str, escalation_model: str,
                 max_usd: float | None, max_tokens: int = ANSWER_MAX_TOKENS, timeout: int = 90) -> list[dict]:
    """Answer every benchmark question through ``answer_stream`` exactly as the service does (router, cheap
    draft, verifier, escalation), recording the route, the total cost of ALL attempts and the latency.

    Checkpointed per question and capped like :func:`answer_candidates`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_rows(path)
    done = {r["id"]: r for r in existing}
    spent = sum(r.get("cost_usd") or 0.0 for r in existing)
    with path.open("a", encoding="utf-8") as sink:
        for q in benchmark:
            if q["id"] in done:
                continue
            if max_usd is not None and spent >= max_usd:
                raise AnswerBudgetExceeded(f"spend ${spent:.3f} reached the ${max_usd:.2f} cap before {q['id']}")
            t0 = time.monotonic()
            events = list(answer_stream(q["q"], driver, embedder, strategy="hybrid", model=model, escalation_model=escalation_model,
                                        max_tokens=max_tokens, timeout=timeout))
            row = _deployed_row(q, events, round(time.monotonic() - t0, 3))
            spent += row["cost_usd"] or 0.0
            sink.write(json.dumps(row) + "\n")
            sink.flush()
            done[q["id"]] = row
    return [done[q["id"]] for q in benchmark if q["id"] in done]


def _deployed_row(q: dict, events: list[dict], latency: float) -> dict:
    terminal = next((e for e in reversed(events) if e["event"] in ("done", "error")), None)
    base = {"id": q["id"], "type": q["type"], "q": q["q"], "answer": "", "cited": [], "hallucinated": [], "valid_ids": [],
            "finish_reason": None, "usage": None, "cost_usd": 0.0, "latency_s": latency, "routed": None, "escalated": False}
    if terminal is None or terminal["event"] == "error":
        e = terminal or {}
        return {**base, "error": e.get("detail", "no terminal event"), "usage": e.get("usage"), "cost_usd": e.get("cost_usd") or 0.0}
    cited = sorted(terminal["citations"])
    return {**base, "answer": terminal["answer"], "cited": cited, "hallucinated": terminal["hallucinated"],
            "valid_ids": sorted(set(cited) - set(terminal["hallucinated"])), "finish_reason": terminal.get("finish_reason"),
            "usage": terminal.get("usage"), "cost_usd": terminal.get("cost_usd") or 0.0, "routed": terminal.get("routed"),
            "escalated": bool(terminal.get("escalated")), "answered_by": terminal.get("answered_by"),
            "escalation_reasons": terminal.get("escalation_reasons"), "checks": terminal.get("checks")}


def failed_checks_of(rows: list[dict]) -> dict[str, list[str]]:
    """Question id -> the names of the answer checks the SERVICE reported as failed for that deployed row (retrieved
    citations, grounded numbers, removal claims, ...). Rows without checks are absent here: see ``rows_without_checks``."""
    return {r["id"]: names for r in rows if (names := failed_check_names(r.get("checks")))}


def score_deployed(rows: list[dict], benchmark: list[dict], judge, *, votes: int = 3, judge_model: str | None = None,
                   as_of: str | None = None) -> dict:
    """Score the deployed configuration: correctness (mechanical + majority-vote judge on the open questions and the
    misattribution probes), citation validity, and how the traffic was routed and what it cost. Escalation is what
    actually happened. ``as_of`` is the data date the judge is told (see ``runner.data_as_of``; None = "not stated").
    ``judge_prompt_version`` names the correctness-judge prompt that graded the open questions (``runner.JUDGE_PROMPT_VERSION``)."""
    score = score_mechanical([{**r, "model": "deployed"} for r in rows], benchmark)
    score["judge_prompt_version"] = JUDGE_PROMPT_VERSION
    score["judged"] = judge_open(rows, benchmark, judge, votes=votes, model=judge_model, as_of=as_of)
    routes: dict[str, int] = {}
    for r in rows:
        routes[r.get("routed") or "none"] = routes.get(r.get("routed") or "none", 0) + 1
    costs = [r["cost_usd"] or 0.0 for r in rows]
    score.pop("escalation_ids", None)   # recomputed without the retrieved contexts: not what the service did
    score.update({"routes": routes, "escalated": sum(1 for r in rows if r.get("escalated")),
                  "escalation_rate": (sum(1 for r in rows if r.get("escalated")) / len(rows)) if rows else 0.0,
                  "escalated_ids": [r["id"] for r in rows if r.get("escalated")],
                  "checks_failed": failed_checks_of(rows),
                  "rows_without_checks": [r["id"] for r in rows if not r.get("checks") and not r.get("error")],
                  "total_cost_usd": sum(costs), "avg_cost_usd": sum(costs) / len(rows) if rows else 0.0})
    return score

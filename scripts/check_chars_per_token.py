"""Are the characters-per-token figures true of the prompts this service sends? (Wave 2: the assumption a paid call's bound rests on)

``serve.estimate.chars_per_token(model)`` turns the characters of a worst-case prompt into the tokens it is billed for, and the
paid-call meter (``serve/meter.py``) bounds every call it records with it. It is PER MODEL (2.0 for Claude Sonnet 5, 2.5 for every
other model, Luna included) and an ASSUMPTION, not a bound, so this script checks each model's figure against everything the
recorded runs can say, OFFLINE (no model, no network, no database), and writes ``artifacts/chars_per_token_check.json``. The ratio
of a row is ``characters of the prompt / prompt tokens the provider billed``; a row is BELOW the assumption when it is under the
figure of ITS OWN model (a Sonnet row at 2.2 holds, a Luna row at 2.2 does not).

What is on disk (``data/processed``, git-ignored) and how each file is used:

* ``eval_runs.v2-baseline.jsonl``: 40 rows (20 questions, hybrid and vector) of the Sonnet 5 baseline (2026-09-26), each with its
  saved CONTEXT and the provider's USAGE in the same row. The one single measurement. The model is not recorded in the row;
  docs/v2/M1_REPORT.md says Sonnet 5 answered the baseline.
* ``bakeoff.jsonl``: other models' answers (Luna, Haiku, ...) to the same 20 hybrid prompts, usage included. A join by question id
  with the baseline's hybrid context: ``eval_runs.v2-baseline.jsonl`` is "the byte-identical prompt the baseline saw" for every
  candidate (``eval/bakeoff.py``).
* ``eval_deployed.v2e.jsonl``: the deployed path (Luna drafts, Sonnet escalations) with usage and no prompt. Joined to the
  baseline context of the same id it is only an APPROXIMATION (another day, another graph state, another context layout and
  template), so its rows are listed, marked approximate and kept out of the verdict.

THE TEMPLATE. The prompt is rebuilt with the answer template the runs were made with: the 2026-07-03 text (833 characters, five
blocks), embedded below with its digest so that no git history is needed. The template served today is about 9,100 characters
and was written after these runs (commits of 2026-09-26 13:11 to 2026-09-27 14:34). Rebuilding the old runs' prompts with
today's template adds some 8,300 characters to each, and raises the baseline's median ratio from 2.91 to 3.81 and its lowest
from 2.10 to 2.90: it flatters the assumption, which is why this script does not do it.

Reads nothing but the three files above and prints no prompt, answer or question. Exit status: 0 = no exact row of a model the
service calls is below that model's assumption; 1 = at least one is (the artifact is written either way); 2 = an input file is
missing.

    uv run python scripts/check_chars_per_token.py --validate-template     # how artifacts/chars_per_token_check.json was made
    uv run python scripts/check_chars_per_token.py [--processed data/processed] [--out artifacts/chars_per_token_check.json]

``--validate-template`` adds the one check that the rebuilt prompts are the prompts that were billed: tiktoken's ``o200k_base``
(bundled with litellm, no download) counts the rebuilt Luna bake-off prompts at 0.998 to 1.000 of the tokens Luna billed.
"""

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

from semigraph.serve.estimate import DEFAULT_CHARS_PER_TOKEN, chars_per_token

REPO = Path(__file__).resolve().parent.parent
PROCESSED = REPO / "data" / "processed"
OUT = REPO / "artifacts" / "chars_per_token_check.json"

BASELINE, BAKEOFF, DEPLOYED = "eval_runs.v2-baseline.jsonl", "bakeoff.jsonl", "eval_deployed.v2e.jsonl"
SOURCES = ("baseline", "bakeoff", "deployed_v2e")
SONNET, LUNA = "anthropic/claude-sonnet-5", "openai/gpt-6-luna"
PRODUCTION_MODELS = (LUNA, SONNET)          # the models the service calls (answer, escalation and planner)


def assumed(model: str) -> float:
    """The characters per token the service assumes for ``model`` (``estimate.chars_per_token``: the default for a model it has
    no figure for). Every row is judged against this, never against one number for all models."""
    return float(chars_per_token(model))

# How the recorded runs were prompted: the five saved blocks (``context_layout.LEGACY_CONTEXT_HEADERS``, a test pins the
# equality) poured into the template that `git show 9820b06:src/semigraph/artifacts/prompts/answer.txt` prints.
RUN_HEADERS = ("RELATIONSHIPS:\n", "\n\nMETRICS:\n", "\n\nACTIVE RISKS:\n", "\n\nDROPPED RISK LINEAGES:\n",
               "\n\nEXCERPTS:\n")
RUN_TEMPLATE = """You are a semiconductor supply-chain analyst. Answer the question using ONLY the context below,
retrieved from SEC filings via a knowledge graph.

Rules:
- Cite evidence after every factual sentence using [chunk_id] (ids appear in the context).
- KNOWN RELATIONSHIPS, REPORTED METRICS and DROPPED RISK LINEAGES come from the knowledge graph.
- If the context does not contain the answer, say so plainly — never fill gaps from memory.
- Be concise. Use bullet lists for enumerations.

QUESTION: {question}

=== KNOWN RELATIONSHIPS ===
{edges_block}

=== REPORTED METRICS (deterministic, from XBRL) ===
{metrics_block}

=== DISCLOSED RISKS (currently active, semantically ranked) ===
{risks_block}

=== RISK LINEAGES DROPPED FROM THE LATEST ANNUAL REPORT (bitemporal layer) ===
{temporal_block}

=== SOURCE EXCERPTS ===
{chunks_block}
"""
RUN_TEMPLATE_SHA256 = "8f5d043b18263551c8b34efb8dcc06c04770f9f10c88ca92be8b2ceb4857aabc"      # of the UTF-8 bytes

NO_USAGE = "no provider-reported usage"
NOT_SPLIT = "context does not split into the five saved blocks"
ESCALATED = "escalated (the usage sums two calls)"

CAVEATS = [
    "A JOIN, not one measurement: the runs saved either a prompt's context (the baseline, which has its usage in the same row, "
    "the only single measurement) or a model's usage on that prompt (the bake-off, the deployed run). The bake-off rows are the "
    "baseline's hybrid context of the same question id joined to another model's usage; the deployed rows are not even that.",
    "The prompts are rebuilt with the answer TEMPLATE the runs were made with (the 2026-07-03 text, 833 characters, embedded in "
    "the script; tiktoken's o200k_base counts the rebuilt Luna prompts at 0.998 to 1.000 of the tokens billed, see "
    "template_validation, which today's template does not: 1.10 to 1.52), not today's (about 9,100 characters, written after "
    "the runs). A regression of billed tokens on characters is NOT a way to test this: the contexts differ in kind, so its "
    "intercept (1,400 to 3,900 tokens) says nothing about the template. Today's template over these runs would have "
    "raised the baseline's median from 2.91 to 3.81 and its lowest from 2.10 to 2.90 characters per token: a ratio taken that "
    "way flatters the assumption.",
    "The deployed run's prompts were never saved (eval_deployed.v2e.jsonl keeps usage and answers only): its rows use the "
    "baseline prompt of the same id as a stand-in (another day, another graph state, another context layout and template), "
    "are marked approximate, and are not part of the verdict. Some of them sit below an assumed figure (see flags); they are "
    "listed, not judged, and they are not evidence that the figures hold either.",
    "The contexts are those of the 20 benchmark questions (prompts of 11 to 64 thousand characters), not a sample of what visitors ask, "
    "and far below the ceilings the estimate prices (106 to 470 thousand characters). The usage is the provider's own token count for "
    "that provider's tokenizer; the models of the bake-off the service never calls are listed, not judged (against the default "
    "figure, 2.5, when flagged).",
    "The model of the baseline rows is not recorded in them: Sonnet 5 per docs/v2/M1_REPORT.md. Its tokenizer is not available "
    "offline, so the Sonnet rows are not validated the way the Luna rows are; they rest on the baseline having been prompted "
    "as the bake-off was (the same contexts and template: the bake-off rebuilt 'the byte-identical prompt the baseline saw').",
]
MISSING = [
    "planner prompts: no run saved the planner's messages and tool schemas next to their usage (eval_agent*.jsonl keep usage "
    "and steps, no prompts), so the planner's characters per token cannot be measured offline",
    "workspace prompts: no run saved an uploaded-document prompt with its usage",
    "the deployed path's own prompts (the 2026-09-27 template and context layout): usage was saved, the prompt was not, so no "
    "ratio of the prompt the service sends today can be computed from the recorded runs",
    "prompts near the estimate's ceiling: the recorded prompts are 11 to 64 thousand characters",
    "the meter writes the missing measurement: PaidMeter.complete logs `meter_ratio model= role= prompt_chars= prompt_tokens=` at "
    "INFO for every completed call, so a preview run produces the ratio of the real prompts, planner and workspace included",
]


# --- reading the runs ---------------------------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _split(context: str) -> tuple[str, ...] | None:
    """The five blocks between the saved headers; None when the context is not in that layout (it never guesses)."""
    first = RUN_HEADERS[0]
    if not context.startswith(first):
        return None
    cursor, starts, blocks = len(first), [len(first)], []
    for header in RUN_HEADERS[1:]:
        at = context.find(header, cursor)
        if at < 0:
            return None
        blocks.append(context[starts[-1]:at])
        cursor = at + len(header)
        starts.append(cursor)
    blocks.append(context[cursor:])
    rebuilt = "".join(h + b for h, b in zip(RUN_HEADERS, blocks, strict=True))
    return tuple(blocks) if rebuilt == context else None


def run_prompt(question: str, context: str) -> str | None:
    """The prompt the recorded runs sent for ``question`` over the saved ``context``, or None when it does not split."""
    blocks = _split(context)
    if blocks is None:
        return None
    edges, metrics, risks, dropped, chunks = blocks
    return RUN_TEMPLATE.format(question=question, edges_block=edges, metrics_block=metrics, risks_block=risks,
                               temporal_block=dropped, chunks_block=chunks)


def _prompt_tokens(row: dict) -> int | None:
    """The provider's own prompt-token count; a client-side estimate (``estimated``) is not evidence of what was billed."""
    usage = row.get("usage")
    if not isinstance(usage, dict) or usage.get("estimated"):
        return None
    tokens = usage.get("prompt_tokens")
    return tokens if type(tokens) is int and tokens > 0 else None


def _row(source: str, model: str, system: str, id_: str, chars: int, tokens: int, basis: str) -> dict:
    return {"source": source, "model": model, "system": system, "id": id_, "chars": chars, "prompt_tokens": tokens,
            "ratio": round(chars / tokens, 4), "basis": basis}


# --- the report ---------------------------------------------------------------------------------------------------

def _summarise(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[float]] = {}
    for r in rows:
        groups.setdefault((r["source"], r["model"], r["system"]), []).append(r["ratio"])
    return [{"source": s, "model": m, "system": sy, "assumed": assumed(m), "n": len(v), "min": min(v),
             "median": round(statistics.median(v), 4), "max": max(v), "below": sum(x < assumed(m) for x in v)}
            for (s, m, sy), v in sorted(groups.items())]


def _verdict(rows: list[dict]) -> dict:
    """Per model the service calls, against that model's own figure: the exact rows only (the approximate ones are not
    evidence either way)."""
    out = {}
    for model in PRODUCTION_MODELS:
        ratios = [r["ratio"] for r in rows if r["model"] == model and r["basis"] == "exact"]
        if ratios:
            out[model] = {"assumed": assumed(model), "rows": len(ratios), "min_ratio": min(ratios),
                          "median_ratio": round(statistics.median(ratios), 4),
                          "below": sum(x < assumed(model) for x in ratios), "holds": min(ratios) >= assumed(model)}
    return out


def _conclusion(verdict: dict) -> str:
    if not verdict:
        return "no exact row of a model the service calls: nothing to check the assumptions against"
    parts = [f"{m}: lowest {v['min_ratio']} over {v['rows']} rows, {v['below']} below {v['assumed']}" for m, v in verdict.items()]
    holds = all(v["holds"] for v in verdict.values())
    return ("the per-model assumptions hold on the recorded prompts (" if holds else
            "the per-model assumptions do NOT all hold on the recorded prompts (") + "; ".join(parts) + ")"


def load_encoder():
    """tiktoken's ``o200k_base`` (Luna's tokenizer, to within 0.2% on the recorded prompts), or None when it is not available
    offline. Importing litellm first points tiktoken at the encodings litellm bundles, so nothing is downloaded."""
    try:
        import litellm  # noqa: F401
        import tiktoken
        return tiktoken.get_encoding("o200k_base")
    except Exception:  # noqa: BLE001 - an optional check: absent is reported, never raised
        return None


def template_validation(bakeoff: list[dict], hybrid: dict, encoder) -> dict:
    """Is the rebuilt prompt the prompt the run billed? Tokenise the rebuilt Luna bake-off prompts with ``o200k_base`` and
    divide by the provider's billed ``prompt_tokens``: 1.0 means the template, the five-block layout and the join are
    right. Today's template, measured the same way on 2026-10-08, gives 1.10 to 1.52 (it adds some 2,800 tokens)."""
    ratios = []
    for r in bakeoff:
        base, tokens = hybrid.get(r["id"]), _prompt_tokens(r)
        prompt = run_prompt(base["q"], base["context"]) if base and r["model"] == LUNA else None
        if prompt is not None and tokens is not None:
            ratios.append(len(encoder.encode(prompt)) / tokens)
    if not ratios:
        return {"available": True, "encoding": "o200k_base", "model": LUNA, "rows": 0}
    return {"available": True, "encoding": "o200k_base", "model": LUNA, "rows": len(ratios), "min": round(min(ratios), 4),
            "mean": round(statistics.mean(ratios), 4), "max": round(max(ratios), 4),
            "meaning": "tokens of the rebuilt prompt (tiktoken) / prompt_tokens billed; 1.0 = the rebuild is the prompt billed"}


def build_report(processed: Path, validate_template: bool = False) -> dict:
    baseline, bakeoff, deployed = (load_jsonl(processed / name) for name in (BASELINE, BAKEOFF, DEPLOYED))
    hybrid = {r["id"]: r for r in baseline if r.get("system") == "hybrid"}
    rows: list[dict] = []
    excluded = {source: Counter() for source in SOURCES}
    unmatched: dict[str, set] = {"bakeoff": set(), "deployed_v2e": set()}

    for r in baseline:
        tokens, prompt = _prompt_tokens(r), run_prompt(r["q"], r["context"])
        if tokens is None:
            excluded["baseline"][NO_USAGE] += 1
        elif prompt is None:
            excluded["baseline"][NOT_SPLIT] += 1
        else:
            rows.append(_row("baseline", SONNET, r["system"], r["id"], len(prompt), tokens, "exact"))

    def joined(source: str, id_: str, tokens: int | None) -> str | None:
        """The baseline hybrid prompt of ``id_`` when the join is possible, else the exclusion is booked."""
        base = hybrid.get(id_)
        if base is None:
            unmatched[source].add(id_)
        elif tokens is None:
            excluded[source][NO_USAGE] += 1
        else:
            prompt = run_prompt(base["q"], base["context"])
            if prompt is None:
                excluded[source][NOT_SPLIT] += 1
            return prompt
        return None

    for r in bakeoff:
        tokens = _prompt_tokens(r)
        prompt = joined("bakeoff", r["id"], tokens)
        if prompt is not None:
            rows.append(_row("bakeoff", r["model"], "hybrid", r["id"], len(prompt), tokens, "exact"))
    for r in deployed:
        if r.get("escalated"):
            excluded["deployed_v2e"][ESCALATED] += 1
            continue
        tokens = _prompt_tokens(r)
        prompt = joined("deployed_v2e", r["id"], tokens)
        if prompt is not None:
            rows.append(_row("deployed_v2e", r["answered_by"], "hybrid", r["id"], len(prompt), tokens, "approximate"))

    verdict = _verdict(rows)
    flags = [{**{k: r[k] for k in ("source", "model", "id", "system", "chars", "prompt_tokens", "ratio", "basis")},
              "assumed": assumed(r["model"]), "production_model": r["model"] in PRODUCTION_MODELS}
             for r in rows if r["ratio"] < assumed(r["model"])]
    inputs = {name: {"rows": len(load_jsonl(processed / name)), "sha256": hashlib.sha256((processed / name).read_bytes()).hexdigest()}
              for name in (BASELINE, BAKEOFF, DEPLOYED)}
    report = {
        "assumption": {"chars_per_token": {m: assumed(m) for m in PRODUCTION_MODELS},
                       "default": float(DEFAULT_CHARS_PER_TOKEN),
                       "where": "semigraph.serve.estimate.chars_per_token(model)",
                       "meaning": "characters of a prompt / this = the tokens it is priced at, per model (the default for a "
                                  "model with no figure of its own); a prompt denser than this (fewer characters per token) "
                                  "costs more than its bound"},
        "method": "ratio = characters of the rebuilt prompt / prompt_tokens the provider billed; 'exact' rows rebuild the "
                  "prompt the run sent (embedded 2026-07-03 template over the five saved blocks); 'approximate' rows stand in "
                  "for a prompt that was not saved",
        "template": {"git": "9820b06:src/semigraph/artifacts/prompts/answer.txt", "chars": len(RUN_TEMPLATE),
                     "sha256": RUN_TEMPLATE_SHA256},
        "caveats": CAVEATS,
        "missing": MISSING,
        "inputs": inputs,
        "excluded": {source: dict(sorted(counts.items())) for source, counts in excluded.items()},
        "unmatched_ids": {source: sorted(ids) for source, ids in unmatched.items()},
        "summary": _summarise(rows),
        "verdict": verdict,
        "conclusion": _conclusion(verdict),
        "flags": flags,
        "rows": rows,
    }
    if validate_template:
        encoder = load_encoder()
        report["template_validation"] = (template_validation(bakeoff, hybrid, encoder) if encoder is not None else
                                         {"available": False, "reason": "tiktoken o200k_base could not be loaded offline"})
    return report


# --- the command line ----------------------------------------------------------------------------------------------

def _print(report: dict) -> None:
    print(report["conclusion"])
    check = report.get("template_validation")
    if check is not None:
        print("  template check:", f"rebuilt Luna prompts / tokens billed {check['min']} to {check['max']} over {check['rows']} rows"
              if check.get("rows") else check.get("reason", "no Luna rows"))
    for s in report["summary"]:
        print(f"  {s['source']:<13} {s['model']:<44} {s['system']:<7} n={s['n']:<3} min {s['min']:<7} "
              f"median {s['median']:<7} max {s['max']:<7} below {s['below']} (of {s['assumed']})")
    for f in report["flags"]:
        kind = "the service calls this model" if f["production_model"] else "not a model the service calls"
        print(f"  BELOW {f['assumed']}: {f['source']} {f['model']} {f['id']} {f['system']} ratio {f['ratio']} "
              f"({f['basis']}; {kind})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the characters-per-token assumption against the recorded runs (offline).")
    parser.add_argument("--processed", type=Path, default=PROCESSED, help="the folder holding the three recorded-run files")
    parser.add_argument("--out", type=Path, default=OUT, help="where the artifact is written")
    parser.add_argument("--validate-template", action="store_true",
                        help="also tokenise the rebuilt Luna prompts (tiktoken o200k_base, bundled with litellm) and compare "
                             "them with the tokens billed: the evidence that the embedded template is the one the runs used")
    args = parser.parse_args(argv)
    missing = [name for name in (BASELINE, BAKEOFF, DEPLOYED) if not (args.processed / name).is_file()]
    if missing:
        print(f"missing input file(s) in {args.processed}: {', '.join(missing)}", file=sys.stderr)
        return 2
    report = build_report(args.processed, validate_template=args.validate_template)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes((json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))      # LF, no clock: stable
    _print(report)
    return 1 if any(f["production_model"] and f["basis"] == "exact" for f in report["flags"]) else 0


if __name__ == "__main__":
    sys.exit(main())

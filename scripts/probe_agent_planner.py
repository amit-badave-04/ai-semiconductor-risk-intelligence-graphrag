"""Live probe of the REAL agent planner inputs (cents): the real tool schemas + the real planner prompt against the candidate planner models.

``scripts/probe_tool_calling.py`` proved the provider handshake with one toy tool. This probe sends what the agent really sends
(``agent.tools.tool_specs()``, ``agent.planner.initial_messages``) for a handful of agent-benchmark questions, once per model, so a schema
feature a provider rejects (a 400 would silently turn every agent answer into the plain fixed path, scored as an agent answer) or an
argument shape the argument models refuse is found BEFORE the paid evaluation run. It also records what the model chose; whether that is
GOOD is what the paid run measures (docs/v2/M3_AGENT_PLAN.md section 3): nothing here gates it.

    .venv/Scripts/python scripts/probe_agent_planner.py [--models openai/gpt-6-luna anthropic/claude-sonnet-5]
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from pydantic import ValidationError  # noqa: E402

from semigraph.agent import tools as agent_tools  # noqa: E402
from semigraph.agent.planner import LiteLLMPlanner, initial_messages  # noqa: E402
from semigraph.llm_shape import KNOWN_PRICES_PER_MTOK  # noqa: E402

BENCHMARK = Path("artifacts/agent_benchmark.json")
OUT = Path("artifacts/agent_planner_probe.json")
QUESTIONS = ("A01", "A10", "A13", "A15", "A17")     # multi-company metrics, a risk change, a no-tool case, a refusal, an injection
TIMEOUT_S = 30.0

# A SYNTHETIC, EMPTY prefetch (no graph is touched): it fixes the shape of the planner's input, not what a real retrieval holds. With
# nothing retrieved, asking for metrics is the right call, so the tools a model chooses here say nothing about over-calling; only
# "the provider accepted the real schemas" and "every argument validates" are checked. The paid run measures the choices.
PREFETCH = {"anchors": {"Nvidia": 1045810}, "edges": [], "metrics": [], "metric_periods": {"years": [], "dates": []}, "risks": [],
            "temporal": [], "temporal_pairs": [], "temporal_passages": [], "temporal_notices": [], "chunks": []}


def _args_valid(name: str, arguments: str) -> tuple[bool, str]:
    model = agent_tools._MODELS.get(name)
    if model is None:
        return False, f"unknown tool {name!r}"
    try:
        model.model_validate_json(arguments or "{}")
    except ValidationError as e:
        return False, str(e).splitlines()[0][:200]
    return True, ""


def probe(model: str, questions: list[dict]) -> dict:
    planner, specs, cases, spent = LiteLLMPlanner(model), agent_tools.tool_specs(), [], 0.0
    price_in, price_out = KNOWN_PRICES_PER_MTOK.get(model, (0.0, 0.0))
    for q in questions:
        try:
            turn = planner(initial_messages(q["q"], PREFETCH), specs, timeout=TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - the probe reports every failure mode; a 400 here is exactly the finding
            cases.append({"id": q["id"], "ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"})
            continue
        usage = turn.usage or {"prompt_tokens": 0, "completion_tokens": 0}
        spent += (usage["prompt_tokens"] * price_in + usage["completion_tokens"] * price_out) / 1e6
        calls = [{"tool": c.name, "arguments": c.arguments, "args_valid": _args_valid(c.name, c.arguments)[0],
                  "problem": _args_valid(c.name, c.arguments)[1]} for c in turn.tool_calls]
        cases.append({"id": q["id"], "ok": all(c["args_valid"] for c in calls), "expected_tools": q.get("expected_tools"),
                      "forbidden_tools": q.get("forbidden_tools"), "calls": calls, "finish_reason": turn.finish_reason, "usage": usage})
    return {"model": model, "cases": cases, "accepted_the_real_schemas": all("error" not in c for c in cases),
            "all_arguments_valid": all(c["ok"] for c in cases), "cost_usd": round(spent, 5)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--models", nargs="+", default=["openai/gpt-6-luna", "anthropic/claude-sonnet-5"])
    args = ap.parse_args(argv)
    bench = {q["id"]: q for q in json.loads(BENCHMARK.read_text(encoding="utf-8"))["questions"]}
    results = [probe(m, [bench[i] for i in QUESTIONS]) for m in args.models]
    OUT.write_text(json.dumps({"tool_names": list(agent_tools.TOOL_NAMES), "results": results}, indent=2), encoding="utf-8")
    for r in results:
        print(f"{r['model']}: schemas accepted={r['accepted_the_real_schemas']} arguments valid={r['all_arguments_valid']} cost=${r['cost_usd']}")
        for c in r["cases"]:
            chosen = [x["tool"] for x in c.get("calls", [])] if "calls" in c else c.get("error", "")
            print(f"  {c['id']}: {'ok' if c['ok'] else 'FAIL'}  chose={chosen}  expected={c.get('expected_tools')}")
    return 0 if all(r["accepted_the_real_schemas"] and r["all_arguments_valid"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

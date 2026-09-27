"""Live probe: does each candidate PLANNER model do tool calling through LiteLLM the way the agent needs?

The agent (docs/v2/M3_AGENT_PLAN.md) is a retrieval planner: a cheap model is given read-only tools, decides which to
call, and the loop feeds the results back. Four behaviours are load-bearing and each is checked per model:

- ``single``   the model answers a company question with a ``tool_calls`` entry naming a declared tool with valid JSON args
- ``parallel`` a two-company question yields two calls in one turn (or two turns): the loop must handle both
- ``roundtrip`` a ``role: tool`` result goes back and the model either ends the turn or asks for one more tool, never errors
- ``none``     ``tool_choice="none"`` yields text and no call (the loop uses it to force the last turn to finish)

Costs about $0.01 per model (capped 300-token replies, no filing text). Writes ``artifacts/agent_tool_probe.json`` so the
decision to use a planner model is a committed measurement, not a memory.

    uv run python scripts/probe_tool_calling.py [--models openai/gpt-6-luna anthropic/claude-sonnet-5]
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import litellm  # noqa: E402  (after load_dotenv: litellm reads provider keys from the environment)

from semigraph.llm_shape import KNOWN_PRICES_PER_MTOK, completion_params  # noqa: E402

TOOLS = [{
    "type": "function",
    "function": {
        "name": "lookup_company",
        "description": "Resolve a company name or ticker to its SEC CIK and the fiscal years the graph holds.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "company name or ticker"}},
                       "required": ["name"], "additionalProperties": False},
    },
}]
SYSTEM = "You plan read-only lookups. Call lookup_company for each company the question names. Never answer from memory."
OUT = Path("artifacts/agent_tool_probe.json")
MAX_TOKENS = 300
# GPT-6 rejects function tools on /v1/chat/completions unless reasoning is off (probed 2026-09-27): the planner runs with "none".
PLANNER_EFFORT = "none"


def _call(model: str, messages: list, **kw) -> tuple[dict, dict]:
    resp = litellm.completion(model=model, messages=messages, tools=TOOLS, timeout=60, **completion_params(model, MAX_TOKENS, reasoning_effort=PLANNER_EFFORT), **kw)
    msg = resp.choices[0].message
    calls = [{"id": c.id, "name": c.function.name, "args": json.loads(c.function.arguments or "{}")} for c in (msg.tool_calls or [])]
    u = resp.usage
    return ({"content": msg.content, "tool_calls": calls, "finish_reason": resp.choices[0].finish_reason},
            {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens})


def _cost(model: str, usage: dict) -> float:
    inp, out = KNOWN_PRICES_PER_MTOK.get(model, (0.0, 0.0))
    return (usage["prompt_tokens"] * inp + usage["completion_tokens"] * out) / 1e6


def probe(model: str) -> dict:
    checks: dict[str, dict] = {}
    spent = 0.0

    def run(name: str, fn):
        nonlocal spent
        try:
            result, usage = fn()
            spent += _cost(model, usage)
            checks[name] = {"ok": result.pop("ok"), **result, "usage": usage}
        except Exception as e:  # noqa: BLE001 - a probe reports every failure mode instead of stopping at the first
            checks[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}

    user1 = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "How exposed is Nvidia to TSMC?"}]

    def single():
        msg, usage = _call(model, user1)
        good = bool(msg["tool_calls"]) and all(c["name"] == "lookup_company" and "name" in c["args"] for c in msg["tool_calls"])
        return {"ok": good, "message": msg}, usage

    def parallel():
        msg, usage = _call(model, [{"role": "system", "content": SYSTEM},
                                   {"role": "user", "content": "Compare the risk factors of Nvidia and AMD."}])
        return {"ok": len(msg["tool_calls"]) >= 1, "n_calls_in_turn": len(msg["tool_calls"]), "message": msg}, usage

    def roundtrip():
        first, u1 = _call(model, user1)
        if not first["tool_calls"]:
            return {"ok": False, "why": "no tool call to answer", "message": first}, u1
        call = first["tool_calls"][0]
        messages = user1 + [
            {"role": "assistant", "content": first["content"],
             "tool_calls": [{"id": call["id"], "type": "function",
                             "function": {"name": call["name"], "arguments": json.dumps(call["args"])}}]},
            {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"company": "Nvidia", "cik": 1045810, "years": [2024, 2025, 2026]})},
        ]
        second, u2 = _call(model, messages)
        usage = {k: u1[k] + u2[k] for k in u1}
        return {"ok": bool(second["content"]) or bool(second["tool_calls"]), "second_turn": second}, usage

    def none():
        msg, usage = _call(model, user1, tool_choice="none")
        return {"ok": not msg["tool_calls"] and bool(msg["content"]), "message": msg}, usage

    for name, fn in (("single", single), ("parallel", parallel), ("roundtrip", roundtrip), ("tool_choice_none", none)):
        run(name, fn)
    return {"model": model, "usable_as_planner": all(checks[k]["ok"] for k in ("single", "roundtrip", "tool_choice_none")),
            "checks": checks, "cost_usd": round(spent, 5)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--models", nargs="+", default=["openai/gpt-6-luna", "anthropic/claude-sonnet-5"])
    args = ap.parse_args(argv)
    results = [probe(m) for m in args.models]
    OUT.write_text(json.dumps({"litellm": litellm.__version__ if hasattr(litellm, "__version__") else None,
                               "results": results}, indent=2), encoding="utf-8")
    for r in results:
        print(f"{r['model']}: usable_as_planner={r['usable_as_planner']} cost=${r['cost_usd']}")
        for name, c in r["checks"].items():
            print(f"  {name}: {'ok' if c['ok'] else 'FAIL'}{'  ' + c['error'] if 'error' in c else ''}")
    return 0 if all(r["usable_as_planner"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

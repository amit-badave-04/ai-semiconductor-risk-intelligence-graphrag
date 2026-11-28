"""M3 writer rules on the PRODUCTION answer models (PAID, about $0.6-0.7 for 3 repeats x 2 models): the cases of
m3_writer_rules.yaml (rendered from the shipped answer.txt by gen_m3.py) sent through the repo's own wrapper (answerer.llm_text)
to the default and the escalation model, each scored with the same assertions the promptfoo file uses (the two javascript
asserts are translated below, nothing else is). The local stand-in model (gpt-oss-20b) is not what ships; this is the evidence
for spec R2-R4 and S2/S5 (docs/specs/m3-agent-fix.md).

    PYTHONPATH=src uv run --no-sync python tests/security/promptfoo/m3_production_check.py \
        --models openai/gpt-6-luna,anthropic/claude-sonnet-5 --repeat 3

Writes m3_production_results.json next to itself (case, model, run, pass, failed assertions, the first 400 characters).
Never prints a key; reads them only through the app's settings.
"""

import argparse
import json
import re
from pathlib import Path

from semigraph.retrieval.answerer import llm_text

HERE = Path(__file__).resolve().parent
JS = {
    "output.split(/[.!?]\\s/).filter(s=>s.trim().length>0).length <= 3":
        lambda out: len([s for s in re.split(r"[.!?]\s", out) if s.strip()]) <= 3,
    "output.split('\\n').filter(l => /^\\s*[-*•]\\s+\\S/.test(l)).length === 2":
        lambda out: len([line for line in out.split("\n") if re.match(r"^\s*[-*•]\s+\S", line)]) == 2,
    "output.trim().length > 0": lambda out: len(out.strip()) > 0,
}


def check(assertion: dict, out: str) -> bool:
    kind, value = assertion["type"], assertion["value"]
    if kind == "icontains":
        return value.lower() in out.lower()
    if kind == "not-icontains":
        return value.lower() not in out.lower()
    if kind == "regex":
        return re.search(value, out) is not None
    if kind == "not-regex":
        return re.search(value, out) is None
    if kind == "javascript":
        return JS[value](out)
    raise ValueError(f"unknown assertion type {kind!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    config = json.loads((HERE / "m3_writer_rules.yaml").read_text(encoding="utf-8"))
    default = config.get("defaultTest", {}).get("assert", [])
    rows = []
    for model in args.models.split(","):
        for test in config["tests"]:
            for run in range(args.repeat):
                out = llm_text(test["vars"]["prompt"], model=model, max_tokens=1200)
                failed = [a.get("value", "")[:60] for a in default + test["assert"] if not check(a, out)]
                rows.append({"case": test["description"], "model": model, "run": run, "pass": not failed, "failed": failed,
                             "answer": out[:400]})
                print(f"{model:28s} {'PASS' if not failed else 'FAIL'}  {test['description']}")
    (HERE / "m3_production_results.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

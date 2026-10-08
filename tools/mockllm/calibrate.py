"""Write ``profiles.json`` (the timing and length profile of the mock) from recorded runs.

    python -m tools.mockllm.calibrate --v2e data/processed/eval_deployed.v2e.jsonl \
        --agent data/processed/eval_agent.v2.jsonl --out tools/mockllm/profiles.json
    python -m tools.mockllm.calibrate --base tools/mockllm/profiles.json --s12 artifacts/s12_latency_smoke.json \
        --out tools/mockllm/profiles.json            # after the S12 live smoke: measured numbers replace the provisional ones

Sources, from weakest to strongest (a later source replaces what it measures):

* ``--v2e``: the deployed-path benchmark rows (``eval_deployed.v2e.jsonl``): per row the answer text, the BILLED usage and the
  whole-ask latency (embedding + retrieval + model). It has no time to first token, so the numbers derived from it are
  PROVISIONAL, and an upper bound: the model's first-token time is the latency minus ``--retrieval-overhead-s`` (an assumption,
  default 0.5 s) minus the time to decode the visible text at the rate a regression of latency on billed tokens gives. Two
  facts are read from the rows rather than assumed: gpt-6-luna reasons before it answers (its billed completion tokens are far
  more than its visible text; the difference is the ``hidden_tokens`` table, and its time is inside ``ttft_s``), and answers full
  of citation ids are ~2.8 characters per token (read from the Sonnet rows, where thinking is off), not the usual 4.
* ``--agent``: agent benchmark rows (``eval_agent.v2.jsonl``): the planner role, per model call = ``agent.elapsed_s`` (which also
  holds the prefetch and the tool runs, so an upper bound) over ``agent.model_calls``.
* ``--s12``: the live smoke (``scripts/latency_smoke.py``): per model call the measured time to first token, decode time,
  chunk count, visible characters and billed usage. Replaces ``ttft_s``, the decode rate and the lengths of a role that has at
  least ``MIN_S12_CALLS`` calls, the chunk interval, and (with at least ``MIN_S12_ASKS`` cheap-routed asks) the escalation rate.

Reads only the files named; prints no row content.
"""

import argparse
import json
import statistics
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

from .profile import DEFAULT_PROFILE_PATH, PROFILE_VERSION, Profile, RoleProfile, Table, load_profile

RETRIEVAL_OVERHEAD_S = 0.5
PROMPT_CHARS_PER_TOKEN = 3.84          # median of 40 recorded answering prompts / billed prompt tokens (eval_runs.v2-baseline)
DEFAULT_VISIBLE_CHARS_PER_TOKEN = 2.8
DEFAULT_CHUNK_INTERVAL_S = 0.04
DEFAULT_SLOW_TTFT_S = 5.0
MIN_TTFT_S = 0.15
MIN_S12_CALLS = 3
MIN_S12_ASKS = 10
TPS_RANGE = (20.0, 1000.0)
DEFAULT_TPS = {"draft": 130.0, "strong": 110.0, "planner": 100.0}
PLANNER_DEFAULT = RoleProfile(Table.constant(0.8), Table.constant(45), Table.constant(0), DEFAULT_TPS["planner"], 0, True,
                              "openai/gpt-6-luna", "default, not measured: no agent rows were given")
NOTES = {"draft": "from v2e rows; ttft_s is an upper bound (includes hidden reasoning time)",
         "strong": "from v2e rows; ttft_s is an upper bound",
         "planner": "from agent rows; ttft_s is an upper bound (the elapsed time holds the prefetch and the tool runs)"}
MODEL_HINTS = {"draft": "openai/gpt-6-luna", "strong": "anthropic/claude-sonnet-5", "planner": "openai/gpt-6-luna"}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _regression_slope(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 5:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    spread = sum((x - mx) ** 2 for x in xs)
    return None if spread == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / spread


def _tps(xs: Sequence[float], ys: Sequence[float], default: float) -> float:
    slope = _regression_slope(xs, ys)
    rate = 1.0 / slope if slope and slope > 0 else default
    return rate if TPS_RANGE[0] <= rate <= TPS_RANGE[1] else default


def _usable(row: dict) -> bool:
    usage = row.get("usage") or {}
    return (isinstance(row.get("answer"), str) and row["answer"] and isinstance(row.get("latency_s"), (int, float))
            and isinstance(usage.get("completion_tokens"), int) and usage["completion_tokens"] > 0)


def _v2e_role(rows: list[dict], role: str, cpt: float, overhead_s: float) -> RoleProfile:
    billed = [r["usage"]["completion_tokens"] for r in rows]
    latency = [r["latency_s"] for r in rows]
    visible = [float(b) if role == "strong" else min(float(b), len(r["answer"]) / cpt) for r, b in zip(rows, billed)]
    hidden = [max(0.0, b - v) for b, v in zip(billed, visible)]
    tps = _tps(billed, latency, DEFAULT_TPS[role])
    ttft = [max(MIN_TTFT_S, lat - overhead_s - v / tps) for lat, v in zip(latency, visible)]
    return RoleProfile(Table.from_values(ttft), Table.from_values(visible), Table.from_values(hidden), tps, len(rows), True,
                       MODEL_HINTS[role], NOTES[role])


def apply_v2e(rows: list[dict], base: Profile | None, overhead_s: float) -> Profile:
    usable = [r for r in rows if _usable(r)]
    draft = [r for r in usable if r.get("routed") == "cheap" and not r.get("escalated")]
    strong = [r for r in usable if r.get("routed") == "strong" and not r.get("escalated")]
    if len(draft) < 5 or len(strong) < 5:
        raise ValueError(f"v2e rows: need at least 5 draft and 5 strong rows, got {len(draft)} and {len(strong)}")
    cpt = round(statistics.median(len(r["answer"]) / r["usage"]["completion_tokens"] for r in strong), 3)
    cheap_routed = [r for r in rows if r.get("routed") == "cheap"]
    escalation = sum(1 for r in cheap_routed if r.get("escalated")) / max(1, len(cheap_routed))
    roles = dict(base.roles) if base else {"planner": PLANNER_DEFAULT}
    roles["draft"], roles["strong"] = _v2e_role(draft, "draft", cpt, overhead_s), _v2e_role(strong, "strong", cpt, overhead_s)
    source = {"kind": "v2e", "rows": len(rows), "draft_rows": len(draft), "strong_rows": len(strong),
              "retrieval_overhead_assumed_s": overhead_s, "provisional": True}
    return _profile(base, roles, escalation_rate=escalation,
                    routed_strong_share=sum(1 for r in rows if r.get("routed") == "strong") / max(1, len(rows)),
                    visible_chars_per_token=cpt, source=source)


def apply_agent(rows: list[dict], base: Profile) -> Profile:
    calls = []
    for row in rows:
        agent = row.get("agent") or {}
        n, usage = agent.get("model_calls"), agent.get("planner_usage") or {}
        if isinstance(n, int) and n > 0 and isinstance(agent.get("elapsed_s"), (int, float)) and usage.get("completion_tokens"):
            calls.append((agent["elapsed_s"] / n, usage["completion_tokens"] / n))
    if len(calls) < MIN_S12_CALLS:
        raise ValueError(f"agent rows: need at least {MIN_S12_CALLS} planner runs, got {len(calls)}")
    planner = RoleProfile(Table.from_values([c[0] for c in calls]), Table.from_values([c[1] for c in calls]),
                          Table.constant(0), DEFAULT_TPS["planner"], len(calls), True, MODEL_HINTS["planner"], NOTES["planner"])
    return _profile(base, {**base.roles, "planner": planner}, source={"kind": "agent", "runs": len(calls), "provisional": True})


def _s12_role(calls: list[dict], role: str, cpt: float, previous: RoleProfile) -> RoleProfile:
    ttft = [c["ttft_s"] for c in calls if isinstance(c.get("ttft_s"), (int, float))]
    billed = [c["completion_tokens"] for c in calls if c.get("completion_tokens")]
    if role == "planner":
        return replace(previous, ttft_s=Table.from_values([c["latency_s"] for c in calls if c.get("latency_s")] or ttft),
                       visible_tokens=Table.from_values(billed or [previous.visible_tokens.median]), n=len(calls),
                       provisional=False, note="measured by S12 (a planner call is not streamed: ttft_s is its whole latency)")
    measured = [c for c in calls if c.get("completion_tokens") and c.get("visible_chars") is not None]
    visible = [min(float(c["completion_tokens"]), c["visible_chars"] / cpt) if role == "draft" else float(c["completion_tokens"])
               for c in measured]
    hidden = [max(0.0, c["completion_tokens"] - v) for c, v in zip(measured, visible)]
    rates = [v / c["decode_s"] for c, v in zip(measured, visible) if c.get("decode_s", 0) >= 0.2 and v >= 20]
    return replace(previous, ttft_s=Table.from_values(ttft), visible_tokens=Table.from_values(visible),
                   hidden_tokens=Table.from_values(hidden),
                   visible_tokens_per_s=statistics.median(rates) if len(rates) >= MIN_S12_CALLS else previous.visible_tokens_per_s,
                   n=len(calls), provisional=False, note="measured by S12 (live models, local graph)")


def apply_s12(doc: dict, base: Profile) -> Profile:
    calls, asks = doc.get("calls") or [], doc.get("asks") or []
    by_role = {role: [c for c in calls if c.get("role") == role] for role in ("draft", "strong", "planner")}
    strong_cpt = [c["visible_chars"] / c["completion_tokens"] for c in by_role["strong"]
                  if c.get("completion_tokens") and c.get("visible_chars")]
    cpt = round(statistics.median(strong_cpt), 3) if len(strong_cpt) >= MIN_S12_CALLS else base.visible_chars_per_token
    roles = dict(base.roles)
    for role, role_calls in by_role.items():
        if len(role_calls) >= MIN_S12_CALLS:
            roles[role] = _s12_role(role_calls, role, cpt, base.roles[role])
    cheap = [a for a in asks if a.get("routed") == "cheap"]
    escalation = (sum(1 for a in cheap if a.get("escalated")) / len(cheap)) if len(cheap) >= MIN_S12_ASKS else base.escalation_rate
    gaps = [c["decode_s"] / (c["n_deltas"] - 1) for c in calls if c.get("n_deltas", 0) >= 5 and c.get("decode_s", 0) > 0]
    interval = round(max(0.005, statistics.median(gaps)), 4) if len(gaps) >= MIN_S12_CALLS else base.chunk_interval_s
    source = {"kind": "s12", "calls": len(calls), "asks": len(asks), "provisional": False}
    return _profile(base, roles, escalation_rate=escalation, visible_chars_per_token=cpt, chunk_interval_s=interval, source=source)


def _profile(base: Profile | None, roles: dict, *, source: dict, **changes) -> Profile:
    previous = base or Profile(roles, 0.0, 0.0, DEFAULT_VISIBLE_CHARS_PER_TOKEN, PROMPT_CHARS_PER_TOKEN,
                               DEFAULT_CHUNK_INTERVAL_S, DEFAULT_SLOW_TTFT_S)
    return replace(previous, roles=roles, sources=(*previous.sources, source), **changes)


def calibrate(*, v2e_rows: list[dict] | None = None, agent_rows: list[dict] | None = None, s12_doc: dict | None = None,
              base: Profile | None = None, retrieval_overhead_s: float = RETRIEVAL_OVERHEAD_S, today: str | None = None) -> Profile:
    profile = base
    if v2e_rows is not None:
        profile = apply_v2e(v2e_rows, profile, retrieval_overhead_s)
    if profile is None:
        raise ValueError("nothing to calibrate from: give --v2e, or --base with --s12")
    if agent_rows is not None:
        profile = apply_agent(agent_rows, profile)
    if s12_doc is not None:
        profile = apply_s12(s12_doc, profile)
    return replace(profile, generated_at=today or date.today().isoformat())


def write_profile(profile: Profile, path: Path) -> None:
    Profile.from_dict(json.loads(json.dumps(profile.to_dict())))        # round-trip: never write what cannot be read back
    path.write_bytes((json.dumps(profile.to_dict(), indent=1) + "\n").encode("utf-8"))          # LF on every platform


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--v2e", type=Path, help="eval_deployed.v2e.jsonl (deployed-path rows)")
    p.add_argument("--agent", type=Path, help="eval_agent.v2.jsonl (agent rows: the planner role)")
    p.add_argument("--s12", type=Path, help="the JSON written by scripts/latency_smoke.py")
    p.add_argument("--base", type=Path, help="an existing profiles.json to refine (default: build from --v2e alone)")
    p.add_argument("--out", type=Path, default=DEFAULT_PROFILE_PATH)
    p.add_argument("--retrieval-overhead-s", type=float, default=RETRIEVAL_OVERHEAD_S,
                   help="seconds of embedding + retrieval inside a v2e row's latency (an assumption)")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _args(argv)
    profile = calibrate(
        v2e_rows=read_jsonl(args.v2e) if args.v2e else None, agent_rows=read_jsonl(args.agent) if args.agent else None,
        s12_doc=json.loads(args.s12.read_text(encoding="utf-8")) if args.s12 else None,
        base=load_profile(args.base) if args.base else None, retrieval_overhead_s=args.retrieval_overhead_s)
    write_profile(profile, args.out)
    print(f"wrote {args.out} (version {PROFILE_VERSION}): "
          + ", ".join(f"{name} n={role.n} {'provisional' if role.provisional else 'measured'}" for name, role in profile.roles.items())
          + f"; escalation_rate={profile.escalation_rate:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The timing and length profile of the mock (``profiles.json``): what a "model" does, as distributions.

A profile holds, per ROLE (``draft`` = the cheap answer model, ``strong`` = the escalation model, ``planner`` = the agent's
tool-calling model), empirical distributions stored as 21-point quantile tables, sampled by inverse CDF with linear
interpolation. It is written by :mod:`tools.mockllm.calibrate` from recorded runs (``data/processed/eval_deployed.v2e.jsonl``
today, the S12 live smoke later) and only read here. The model of a request picks its role (:func:`role_of`): a request
with ``tools`` is the planner, a model whose id mentions ``sonnet`` / ``strong`` / ``opus`` is the strong model, anything
else is the draft model.

What a role says about a reply (see ``server`` for how it is played back):

* ``ttft_s``: seconds from the request to the first VISIBLE token. For a model that reasons first (gpt-6-luna does, by
  default) this includes the hidden reasoning time;
* ``visible_tokens``: the length of the text the client sees, in tokens of ``visible_chars_per_token`` characters;
* ``hidden_tokens``: reasoning tokens that are billed (``usage.completion_tokens`` and
  ``completion_tokens_details.reasoning_tokens``) but never streamed;
* ``visible_tokens_per_s``: the decode rate of the visible text.

Top level: ``escalation_rate`` (the share of draft answers the real verifier rejected: the default of the mock's
``invalid_id_rate`` knob), ``routed_strong_share`` (informational: the router, not the mock, decides who gets a question),
``visible_chars_per_token`` (answers full of citation ids are about 2.8 characters per token, not the usual 4),
``prompt_chars_per_token``, ``chunk_interval_s`` (seconds between two streamed chunks) and ``slow_ttft_s`` (the
``slow_ttft_rate`` knob's delay).
"""

import json
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PROFILE_VERSION = 1
QUANTILE_POINTS = 21
ROLES = ("draft", "strong", "planner")
DEFAULT_PROFILE_PATH = Path(__file__).with_name("profiles.json")
STRONG_MARKERS = ("sonnet", "strong", "opus")


@dataclass(frozen=True)
class Table:
    """An empirical distribution: ``q[i]`` is the value at quantile ``i / (len(q) - 1)``; ``n`` is the sample size."""

    q: tuple[float, ...]
    n: int

    @classmethod
    def from_values(cls, values: list[float]) -> "Table":
        if not values:
            raise ValueError("a distribution needs at least one value")
        ordered = sorted(float(v) for v in values)
        last = len(ordered) - 1
        points = []
        for i in range(QUANTILE_POINTS):
            pos = i / (QUANTILE_POINTS - 1) * last
            low = int(math.floor(pos))
            high = min(low + 1, last)
            points.append(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))
        return cls(tuple(round(p, 6) for p in points), len(ordered))

    @classmethod
    def constant(cls, value: float) -> "Table":
        return cls((float(value),) * QUANTILE_POINTS, 0)

    @classmethod
    def from_dict(cls, raw: Mapping) -> "Table":
        q = raw.get("q")
        if not isinstance(q, list) or len(q) != QUANTILE_POINTS or not all(isinstance(v, (int, float)) for v in q):
            raise ValueError(f"a distribution needs {QUANTILE_POINTS} quantile values, got {q!r}")
        if any(b < a for a, b in zip(q, q[1:])):
            raise ValueError("quantile values must not decrease")
        return cls(tuple(float(v) for v in q), int(raw.get("n", 0)))

    def to_dict(self) -> dict:
        return {"n": self.n, "q": list(self.q)}

    def at(self, u: float) -> float:
        """The value at quantile ``u`` (0..1)."""
        pos = min(max(u, 0.0), 1.0) * (len(self.q) - 1)
        low = int(math.floor(pos))
        high = min(low + 1, len(self.q) - 1)
        return self.q[low] + (self.q[high] - self.q[low]) * (pos - low)

    def sample(self, rng: random.Random) -> float:
        return self.at(rng.random())

    @property
    def median(self) -> float:
        return self.at(0.5)


@dataclass(frozen=True)
class RoleProfile:
    ttft_s: Table
    visible_tokens: Table
    hidden_tokens: Table
    visible_tokens_per_s: float
    n: int = 0
    provisional: bool = True
    model_hint: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping) -> "RoleProfile":
        rate = float(raw["visible_tokens_per_s"])
        if not rate > 0:
            raise ValueError(f"visible_tokens_per_s must be positive, got {rate}")
        return cls(Table.from_dict(raw["ttft_s"]), Table.from_dict(raw["visible_tokens"]),
                   Table.from_dict(raw["hidden_tokens"]), rate, int(raw.get("n", 0)), bool(raw.get("provisional", True)),
                   str(raw.get("model_hint", "")), str(raw.get("note", "")))

    def to_dict(self) -> dict:
        return {"model_hint": self.model_hint, "n": self.n, "provisional": self.provisional, "note": self.note,
                "ttft_s": self.ttft_s.to_dict(), "visible_tokens_per_s": round(self.visible_tokens_per_s, 3),
                "visible_tokens": self.visible_tokens.to_dict(), "hidden_tokens": self.hidden_tokens.to_dict()}


@dataclass(frozen=True)
class Profile:
    roles: Mapping[str, RoleProfile]
    escalation_rate: float
    routed_strong_share: float = 0.0
    visible_chars_per_token: float = 2.8
    prompt_chars_per_token: float = 3.8
    chunk_interval_s: float = 0.04
    slow_ttft_s: float = 5.0
    sources: tuple[dict, ...] = field(default_factory=tuple)
    generated_at: str = ""

    def role(self, name: str) -> RoleProfile:
        return self.roles[name]

    @classmethod
    def from_dict(cls, raw: Mapping) -> "Profile":
        if raw.get("version") != PROFILE_VERSION:
            raise ValueError(f"unsupported profile version {raw.get('version')!r} (this code reads {PROFILE_VERSION})")
        roles = {name: RoleProfile.from_dict(raw["roles"][name]) for name in ROLES}
        escalation = float(raw["escalation_rate"])
        if not 0.0 <= escalation <= 1.0:
            raise ValueError(f"escalation_rate must be within 0..1, got {escalation}")
        for key in ("visible_chars_per_token", "prompt_chars_per_token", "chunk_interval_s", "slow_ttft_s"):
            if not float(raw[key]) > 0:
                raise ValueError(f"{key} must be positive, got {raw[key]!r}")
        return cls(roles, escalation, float(raw.get("routed_strong_share", 0.0)), float(raw["visible_chars_per_token"]),
                   float(raw["prompt_chars_per_token"]), float(raw["chunk_interval_s"]), float(raw["slow_ttft_s"]),
                   tuple(raw.get("sources", ())), str(raw.get("generated_at", "")))

    def to_dict(self) -> dict:
        return {"version": PROFILE_VERSION, "generated_at": self.generated_at, "sources": list(self.sources),
                "escalation_rate": round(self.escalation_rate, 6), "routed_strong_share": round(self.routed_strong_share, 6),
                "visible_chars_per_token": self.visible_chars_per_token, "prompt_chars_per_token": self.prompt_chars_per_token,
                "chunk_interval_s": self.chunk_interval_s, "slow_ttft_s": self.slow_ttft_s,
                "roles": {name: self.roles[name].to_dict() for name in ROLES}}


def load_profile(path: Path | str | None = None) -> Profile:
    source = Path(path) if path else DEFAULT_PROFILE_PATH
    return Profile.from_dict(json.loads(source.read_text(encoding="utf-8")))


def role_of(model: str, has_tools: bool) -> str:
    """``planner`` for a request that carries tools, else ``strong`` for a strong model id, else ``draft``."""
    if has_tools:
        return "planner"
    lowered = (model or "").lower()
    return "strong" if any(marker in lowered for marker in STRONG_MARKERS) else "draft"

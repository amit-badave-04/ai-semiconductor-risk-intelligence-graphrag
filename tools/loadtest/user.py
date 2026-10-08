"""One virtual user's behaviour, free of locust: the pre-registered iteration, its pacing and the upload cycle. Pure stdlib.

``locustfile.py`` is glue around this module, so everything the traffic model says is unit-tested against the fake API:

* **the iteration** (``VirtualUser.run_iteration``): the shell, ``/api/stats``, ``/api/examples``, ``/api/freshness`` (plus the
  static bundle on the first iteration of the VU), then EXACTLY ONE ask by the 45 / 40 / 10 / 5 mix, then an evidence lookup
  20 % of the time (a citation of the answer just received when it has one) and a dossier or risk-changes read 5 % of the time;
* **the asks**: a ``cached`` ask repeats an example listed by the live ``/api/examples`` response UNSALTED (it must hit the
  answer cache); every live ask, whatever its class, is salted (council 5), so none of them can hit it;
* **the pacing** (``wait_time``): the cycle length is drawn when the iteration STARTS and only the remainder is slept
  (see ``model.py`` for why Locust's ``between`` would miss the VOID floor by construction); an overrun starts the next
  iteration at once and is recorded;
* **the upload VU** (``UploadVirtualUser``): one upload cycle per period, start to start, staggered at spawn.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from tools.loadtest import model
from tools.loadtest.client import LoadClient
from tools.loadtest.pool import Pool
from tools.loadtest.salt import Salter


@dataclass(frozen=True)
class UserParams:
    think_min_s: float = model.THINK_MIN_S
    think_max_s: float = model.THINK_MAX_S
    upload_period_s: float = model.UPLOAD_PERIOD_S
    static_paths: tuple[str, ...] = ()
    mix: tuple[tuple[str, float], ...] = model.ASK_MIX
    p_evidence: float = model.P_EVIDENCE
    p_dossier: float = model.P_DOSSIER_OR_CHANGES

    def as_dict(self) -> dict:
        return {"think_min_s": self.think_min_s, "think_max_s": self.think_max_s, "upload_period_s": self.upload_period_s,
                "static_paths": list(self.static_paths), "mix": dict(self.mix), "p_evidence": self.p_evidence,
                "p_dossier": self.p_dossier}


class VirtualUser:
    def __init__(self, client: LoadClient, pool: Pool, salter: Salter, rng: random.Random, params: UserParams, *,
                 clock: Callable[[], float] = time.monotonic,
                 on_stats: Callable[[dict], None] | None = None) -> None:
        self.client, self.pool, self.salter, self.rng, self.params, self.clock = client, pool, salter, rng, params, clock
        self.on_stats = on_stats
        self.examples: list[str] = []
        self.iterations = 0
        self._started = clock()
        self._target = 0.0

    # -- pacing ---------------------------------------------------------------------------------------------------------------

    def start_delay(self) -> float:
        """Sleep this long after spawning, before the first iteration (the renewal process's residual life)."""
        return model.first_delay(self.rng, self.params.think_min_s, self.params.think_max_s)

    def wait_time(self) -> float:
        """What to sleep after the iteration that just ran so that it started ``target`` seconds after the previous one."""
        return model.wait_after(self.clock() - self._started, self._target)

    # -- one iteration -----------------------------------------------------------------------------------------------------------

    def run_iteration(self) -> dict:
        self._started, self._target = self.clock(), model.draw_cycle(self.rng, self.params.think_min_s, self.params.think_max_s)
        first = self.iterations == 0
        self.iterations += 1
        c = self.client
        c.read("/", label="shell")
        if first:
            for path in self.params.static_paths:
                c.read(path, label="static")
        _, stats = c.read("/api/stats", label="stats", want_json=True)
        if first and stats and self.on_stats is not None:
            self.on_stats(stats)
        _, listed = c.read("/api/examples", label="examples", want_json=True)
        c.read("/api/freshness", label="freshness")
        self.examples = [e["question"] for e in (listed or {}).get("examples", []) if e.get("question")] or [
            e["question"] for e in self.pool.examples]
        klass = model.choose_ask_class(self.rng, self.params.mix)
        ask, done = self._ask(klass)
        if self.rng.random() < self.params.p_evidence:
            self._evidence((done or {}).get("citations") or [])
        if self.rng.random() < self.params.p_dossier:
            self._dossier_or_changes()
        elapsed = self.clock() - self._started
        c.emit("iteration", n=self.iterations, klass=klass, target_s=round(self._target, 3), elapsed_s=round(elapsed, 3),
                overran=elapsed > self._target)
        return {"klass": klass, "ask": ask}

    def _ask(self, klass: str) -> tuple[dict, dict | None]:
        strategy = model.STRATEGY_FOR[klass]
        if klass == "cached":
            return self.client.ask(klass, self.examples[self.rng.randrange(len(self.examples))], strategy)
        base = self.pool.pick(self.rng, klass)
        salted = self.salter.next(base.text)
        return self.client.ask(klass, salted.text, strategy, salt=salted.token)

    def _evidence(self, citations: Sequence[str]) -> None:
        ids = list(citations) or list(self.pool.evidence_ids)
        if ids:
            evidence_id = ids[self.rng.randrange(len(ids))]
            self.client.read(f"/api/evidence/{quote(evidence_id, safe=':')}", label="evidence")

    def _dossier_or_changes(self) -> None:
        if not self.pool.tickers:
            return
        ticker = self.pool.tickers[self.rng.randrange(len(self.pool.tickers))]
        kind = "dossier" if self.rng.random() < 0.5 else "risk-changes"
        self.client.read(f"/api/company/{ticker}/{kind}", label=kind)


class UploadVirtualUser:
    """The upload population: one upload cycle every ``upload_period_s`` seconds, start to start."""

    def __init__(self, client: LoadClient, params: UserParams, rng: random.Random, *, stagger_slot: int = 0,
                 slots: int = model.UPLOAD_VUS, clock: Callable[[], float] = time.monotonic) -> None:
        self.client, self.params, self.rng, self.clock = client, params, rng, clock
        self.slot = stagger_slot % max(slots, 1)
        self.slots = max(slots, 1)
        self.cycles = 0
        self._started = clock()

    def start_delay(self) -> float:
        """Spread the uploaders over the period (the server runs ONE upload job at a time) with a little jitter."""
        return self.slot * self.params.upload_period_s / self.slots + self.rng.uniform(0.0, 0.02 * self.params.upload_period_s)

    def run_cycle(self) -> dict:
        self._started = self.clock()
        self.cycles += 1
        return self.client.upload_cycle(self.cycles)

    def wait_time(self) -> float:
        return model.wait_after(self.clock() - self._started, self.params.upload_period_s)

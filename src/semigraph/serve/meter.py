"""The per-ask paid-call meter (Wave 2, docs/v2/research/m5-councils/council4/verdict.md, option C).

An ask is admitted against an ESTIMATE (``serve.estimate``: what the dearest ask of its type could cost) and, until now,
an ask whose client went away was charged that whole estimate. One meter per ask changes what the abandoned ask is
charged, never what it is admitted against: every paid model call is recorded the moment it STARTS, with the most that
call could cost (its BOUND: the worst-case prompt at the model's characters per token, ``estimate.chars_per_token``,
plus the full output cap, at the model's price, times the provider attempts it may make), and with the usage the
provider reports when it ends. The lease is settled at::

    charge = max(reported, min(estimate, the sum over the started calls of (reported cost, or the bound while it has none)))

where ``reported`` is the sum of the costs the provider reported. The ledger never records LESS than the provider
billed; the estimate caps only what is a bound. A report above the estimate is charged as reported (and logged at
WARNING, ``meter_over_estimate``): the estimate was not the ceiling it was priced as, and the day's spend and the
address's share must show it.

* no paid call started: **0** (the ask still counts against the daily count, the address's count and the paid window);
* a call that started and has no usage yet is charged its whole bound, up to the estimate: the provider may bill it to
  the end whatever the client did, and the bound is the most that can be;
* a call that declared more than one provider attempt (``attempts``) and then reports usage is charged that usage PLUS
  the bound of its other attempts: a retry reports the usage of its last attempt only, and whether the failed ones were
  billed is not known (``estimate`` lists it as an unverified assumption), so the earlier attempts stay at their bound;
* a record that cannot be trusted (a completion for a call that never started, a second completion, a call whose bound
  cannot be computed) charges the **whole estimate**, or the reported cost when that is higher: the meter fails closed,
  and never raises into the answer path;
* a usage that is estimated by the client library, missing, or not two whole token counts keeps the bound (it is not
  evidence of what was billed).

What the meter cannot do: a call that STARTS after the ask was settled (a planner thread that outlived its join) is
counted and logged at ERROR (``paid call after settlement``) but the lease is already settled and is not settled again.
The ``meter_ratio`` INFO line (model, prompt characters, provider prompt tokens) is the data for the one assumption a
bound rests on, characters per token (per model: see ``estimate.SONNET_CHARS_PER_TOKEN`` for what was and was not
measured); ``meter_over_bound`` is logged at WARNING when a reported cost exceeds its bound.

Thread-safe (one lock; every method is synchronous, O(calls) at worst, and never waits on anything): the planner calls
it from its own thread, the answer stream from the event loop. Pure: no litellm, no I/O. Prices come from
``estimate.resolve_price`` (integer micro-dollars per million tokens), so all arithmetic is in integers rounded up once
per call, as the estimate rounds; ``state.usd_to_micro`` (float dollars to micro-dollars) is therefore not needed here,
and importing the state package would pull the Neo4j driver into a module that must stay light.
"""

import logging
import threading
from dataclasses import dataclass

from ..config import Settings
from .estimate import MICRO_PER_MTOK, resolve_price, tokens_for_chars

logger = logging.getLogger("semigraph.serve.meter")

ROLES = ("draft", "strong", "planner")      # the roles ``estimate.resolve_price`` prices


@dataclass(frozen=True)
class CallRecord:
    """A snapshot of one metered call. ``bound_micro`` is 0 for a call whose bound could not be computed (the meter then
    charges the whole estimate); ``late`` is True for a call that started after :meth:`PaidMeter.close`."""

    call_id: int
    role: str
    model: str
    prompt_chars: int
    bound_micro: int
    reported_micro: int | None
    late: bool


@dataclass(slots=True)
class _Call:
    role: str
    model: str
    prompt_chars: int
    bound_micro: int
    price_in: int                       # micro-dollars per million tokens (0 with the bound when it was not computed)
    price_out: int
    sound: bool                         # the bound was computed from valid input
    late: bool
    extra_micro: int = 0                # the attempts after the first (bound minus one attempt's bound): kept on top of a
                                        # reported usage, which covers one attempt only
    completed: bool = False
    reported_micro: int | None = None


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _whole(value: object, *, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _request_fault(role: object, model: object, prompt_chars: object, max_output_tokens: object,
                   attempts: object) -> str | None:
    """Why a call's bound cannot be computed, or None. The names are fixed words: they are logged, and no caller data is."""
    if role not in ROLES:
        return "bad_role"
    if not (isinstance(model, str) and model):
        return "bad_model"
    if not _whole(prompt_chars, minimum=0):
        return "bad_prompt_chars"
    if not _whole(max_output_tokens, minimum=0):
        return "bad_max_output_tokens"
    if not _whole(attempts, minimum=1):
        return "bad_attempts"
    return None


def _token_counts(usage: object) -> tuple[int, int] | None:
    """``(prompt_tokens, completion_tokens)`` of a usage the provider reported, else None: a client-side estimate
    (``estimated``), a missing usage and anything that is not two whole non-negative counts are not evidence of what was
    billed."""
    if not isinstance(usage, dict) or usage.get("estimated"):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if _whole(prompt, minimum=0) and _whole(completion, minimum=0):
        return prompt, completion
    return None


class PaidMeter:
    """One ask's paid calls. ``settings`` supplies the list prices of a model that has none of its own
    (``estimate.resolve_price``)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = threading.Lock()
        self._calls: dict[int, _Call] = {}
        self._faults: list[str] = []
        self._next_id = 1
        self._closed = False

    # ---- recording ----------------------------------------------------------------------------------------------

    def start(self, *, role: str, model: str, prompt_chars: int, max_output_tokens: int, attempts: int = 1) -> int:
        """Record a paid call about to be made and return its id. ``attempts`` is the number of provider attempts the one
        call may make (``1 + num_retries``); a caller that makes each attempt itself starts each one separately. The
        bound is ``attempts x ceil((tokens x input price + max_output_tokens x output price) / 1e6)`` micro-dollars,
        where ``tokens`` is ``ceil(prompt_chars / the model's characters per token)``
        (``estimate.chars_per_token(model, role)``: 2.0 for Claude Sonnet 5, 2.5 for any other model, a mock model being
        the real model it stands for), the same arithmetic as the estimate's. Never raises: a call whose bound cannot be
        computed is recorded as a fault (the charge is then the whole estimate) and the call goes ahead. Call it
        immediately before the provider is, with no await between."""
        fault = _request_fault(role, model, prompt_chars, max_output_tokens, attempts)
        price_in = price_out = bound = extra = 0
        if fault is None:
            try:
                price = resolve_price(model, role, self._settings)
            except Exception:  # noqa: BLE001 - a price that cannot be read must fail the charge closed, not the answer
                fault = "bad_price"
            else:
                price_in, price_out = price.input, price.output
                one_attempt = tokens_for_chars(prompt_chars, model, role) * price_in + max_output_tokens * price_out
                bound = _ceil_div(attempts * one_attempt, MICRO_PER_MTOK)
                extra = bound - _ceil_div(one_attempt, MICRO_PER_MTOK)
        with self._lock:
            call_id, self._next_id = self._next_id, self._next_id + 1
            late = self._closed
            self._calls[call_id] = _Call(role if role in ROLES else "?", model if isinstance(model, str) else "?",
                                         prompt_chars if _whole(prompt_chars, minimum=0) else 0, bound, price_in,
                                         price_out, fault is None, late, extra)
            if fault is not None:
                self._faults.append(fault)
        if fault is not None:
            logger.error("meter_fault reason=%s call=%d", fault, call_id)
        if late:
            logger.error("paid call after settlement: role=%s model=%s call=%d (its lease was already settled)",
                         role, model, call_id)
        return call_id

    def complete(self, call_id: int, usage: dict | None) -> None:
        """Record that a call ended (whatever it ended with) and the usage the provider reported, or None. A provider
        usage replaces the call's bound with the reported cost, rounded up to the micro-dollar, plus the bound of the
        call's attempts after the first (none when it declared one attempt); anything else keeps the whole bound. A
        completion for an unknown call, or a second one, is a fault."""
        fault, logged = None, None
        with self._lock:
            call = self._calls.get(call_id) if type(call_id) is int else None
            if call is None:
                fault = "complete_unknown_call"
            elif call.completed:
                fault = "complete_twice"
            else:
                call.completed = True
                counts = _token_counts(usage) if call.sound else None
                if counts is not None:
                    call.reported_micro = _ceil_div(counts[0] * call.price_in + counts[1] * call.price_out,
                                                    MICRO_PER_MTOK)
                    logged = (call.model, call.role, call.prompt_chars, counts[0], call.reported_micro,
                              call.bound_micro - call.extra_micro)
            if fault is not None:
                self._faults.append(fault)
        if fault is not None:
            logger.error("meter_fault reason=%s call=%s", fault, call_id if type(call_id) is int else "?")
        elif logged is not None:
            self._log_reported(*logged)

    @staticmethod
    def _log_reported(model: str, role: str, prompt_chars: int, prompt_tokens: int, reported: int, bound: int) -> None:
        logger.info("meter_ratio model=%s role=%s prompt_chars=%d prompt_tokens=%d", model, role, prompt_chars,
                    prompt_tokens)
        if reported > bound:
            logger.warning("meter_over_bound model=%s role=%s reported_micro=%d bound_micro=%d prompt_chars=%d "
                           "prompt_tokens=%d: the prompt was denser than the characters-per-token assumption",
                           model, role, reported, bound, prompt_chars, prompt_tokens)

    def close(self) -> None:
        """The ask is being settled. A call that starts after this is still recorded, but marked ``late`` and logged at
        ERROR. Idempotent."""
        with self._lock:
            self._closed = True

    # ---- reading ------------------------------------------------------------------------------------------------

    def charge_micro(self, estimate_micro: int) -> int:
        """What the ask is charged, in micro-dollars: never less than the cost the provider REPORTED, and otherwise capped
        at ``estimate_micro``. ``reported`` is the sum of the reported costs of the finished calls; ``total`` is the sum
        over the started calls of the reported cost (plus the bound of its other attempts) or, while a call has none, its
        bound::

            no call started and no fault     0
            a fault                          max(estimate, reported)
            otherwise                        max(reported, min(estimate, total))

        The estimate caps what is only a BOUND (a call still running, an attempt whose billing is unknown); it never caps
        a report. A provider's own bill above the estimate means the estimate was not the ceiling it was priced as: the
        day's spend and the address's share must hold what was billed (the ledger records at least what the ask
        cost), and it is logged."""
        if type(estimate_micro) is not int or estimate_micro < 0:
            raise ValueError(f"estimate_micro must be a whole number of micro-dollars >= 0, got {estimate_micro!r}")
        with self._lock:
            reported = sum(c.reported_micro for c in self._calls.values() if c.reported_micro is not None)
            if self._faults:
                total = None
            elif not self._calls:
                return 0
            else:
                total = sum(c.bound_micro if c.reported_micro is None else c.reported_micro + c.extra_micro
                            for c in self._calls.values())
        if reported > estimate_micro:
            logger.warning("meter_over_estimate reported_micro=%d estimate_micro=%d: the reported cost is charged",
                           reported, estimate_micro)
        return max(reported, estimate_micro if total is None else min(estimate_micro, total))

    @property
    def calls(self) -> tuple[CallRecord, ...]:
        """The calls started so far, in start order, as frozen snapshots."""
        with self._lock:
            return tuple(CallRecord(call_id, c.role, c.model, c.prompt_chars, c.bound_micro, c.reported_micro, c.late)
                         for call_id, c in self._calls.items())

    @property
    def faults(self) -> tuple[str, ...]:
        """The records that cannot be trusted, in the order they happened (fixed words; empty when all is well)."""
        with self._lock:
            return tuple(self._faults)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def __repr__(self) -> str:                      # never prints a model or a prompt length
        with self._lock:
            return f"PaidMeter(calls={len(self._calls)}, faults={len(self._faults)}, closed={self._closed})"


__all__ = ["ROLES", "CallRecord", "PaidMeter"]

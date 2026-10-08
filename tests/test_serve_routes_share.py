"""The routes' side of the per-address spend share (Wave 2 step 0; docs/v2/research/m5-councils/council4/verdict.md).

* every ``Denied`` member has an answer in ``routes._DENIALS`` (an unmapped one is a KeyError, i.e. a 500, for a visitor);
* the share's two refusals: ``IP_SPEND`` is its own message (the address's settled spend is used up: NOT the "paused"
  wording), ``IP_SPEND_INFLIGHT`` is the existing busy/retry message;
* a lease no stream took (a cancelled or failed admission) is settled with a cost of 0: no twin ran, so no paid call;
* the gate order in the module docstring names the new gate.

No server and no Neo4j: ``_admit`` and ``_abandon`` run against a recording backend double."""

import anyio
import pytest
from fastapi import HTTPException
from serve_state_fakes import fresh_drain  # noqa: F401 - a fixture: a new process-wide DRAIN for one test

from semigraph.serve import routes
from semigraph.serve.state import Denied, Lease

DAY = "2026-10-08"
MSG_IP_SPEND = "This network's live allowance for today is used — the cached examples still work."


class RecordingBackend:
    def __init__(self, outcome=None):
        self.outcome, self.reconciled = outcome, []

    def reserve(self, **kwargs):
        if self.outcome is not None:
            return self.outcome
        return Lease("lease-1", DAY, kwargs["ip_hash"], kwargs["strategy"], kwargs["workspace"],
                     kwargs["estimate_micro"], 1000.0, "m1")

    def reconcile(self, lease_id, *, outcome, usage, cost_micro):
        self.reconciled.append((lease_id, outcome, usage, cost_micro))
        return True


def app_state(backend):
    limiters = type("L", (), {"state": anyio.CapacityLimiter(2)})()
    settings = type("S", (), {"state_op_timeout_s": 1.0})()
    return type("St", (), {"state": backend, "limiters": limiters, "settings": settings})()


def test_every_denial_has_an_answer_so_no_refusal_can_be_a_server_error():
    assert set(routes._DENIALS) == set(Denied)                              # noqa: SLF001


def test_the_share_refusals_are_429_with_their_own_messages():
    assert routes.MSG_IP_SPEND == MSG_IP_SPEND
    assert routes._DENIALS[Denied.IP_SPEND] == (429, MSG_IP_SPEND)           # noqa: SLF001
    assert routes._DENIALS[Denied.IP_SPEND_INFLIGHT] == (429, routes.MSG_BUSY)   # noqa: SLF001


def test_the_used_up_message_is_not_the_paused_or_budget_wording_and_promises_the_cached_examples():
    others = {routes.MSG_PAUSED, routes.MSG_BUDGET, routes.MSG_IP_BUDGET, routes.MSG_BUSY, routes.MSG_RETRIEVAL_ONLY}
    assert routes.MSG_IP_SPEND not in others
    assert "paused" not in routes.MSG_IP_SPEND.lower() and "cached examples still work" in routes.MSG_IP_SPEND


@pytest.mark.parametrize("denial, detail", [(Denied.IP_SPEND, MSG_IP_SPEND),
                                            (Denied.IP_SPEND_INFLIGHT, routes.MSG_BUSY)])
def test_admit_turns_a_share_denial_into_a_429_and_gives_the_drain_count_back(denial, detail, fresh_drain):  # noqa: F811
    st = app_state(RecordingBackend(denial))

    async def main():
        with pytest.raises(HTTPException) as refused:
            await routes._admit(st, "iph", "hybrid", False, 60_000)          # noqa: SLF001
        assert (refused.value.status_code, refused.value.detail) == (429, detail)
        assert fresh_drain.active == 0                                       # the count was given back

    anyio.run(main)


def test_a_lease_no_stream_took_is_settled_as_abandoned_at_a_cost_of_zero():
    backend = RecordingBackend()
    st = app_state(backend)
    lease = Lease("lease-1", DAY, "iph", "hybrid", False, 60_000, 1000.0, "m1")
    anyio.run(routes._abandon, st, [lease])                                 # noqa: SLF001
    assert backend.reconciled == [("lease-1", "abandoned", None, 0)]


def test_the_gate_order_names_the_share_between_the_per_address_cap_and_the_inflight_cap():
    doc = routes.__doc__
    assert doc.index("MSG_IP_BUDGET") < doc.index("MSG_IP_SPEND") < doc.index("MSG_BUSY")

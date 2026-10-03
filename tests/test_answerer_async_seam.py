"""M5a I2 seam: the async twin replays the pre-M4 SEC recording event for event (docs/v2/M5A_BUILD_PLAN.md section 4).

``tests/data/answer_events_pre_m4.json`` was recorded from the sync writer before M4 (see
``test_answerer_prompt_seam.py``): the events of six scenarios and a hash of every prompt a model received. The
async twin has to reproduce it exactly, because the route that streams it to the browser must not change a byte of
what the sync writer sent.

The scripted streams, the retrieval dict and the scenarios are the ones of ``test_answerer_prompt_seam.py``
(imported, not copied): this file only turns each scripted ``FakeStream`` into an async stream with the same parts,
usage, model, finish reason and failure, so the two replays cannot drift apart. The recording is never re-recorded
from here.
"""

import asyncio
import json
from contextlib import aclosing

import anyio
import pytest
from test_answerer_prompt_seam import RECORDING, FakeStream, _retrieval, _scenarios
from test_answerer_prompt_seam import _run as sync_run

from semigraph.retrieval import answerer_async
from semigraph.serve.limiters import make_limiters

NAMES = [name for name, _, _ in _scenarios()]


class AsyncReplay:
    """An async stream with the script of a sync ``FakeStream`` (which already holds its parts, usage, model and
    failure)."""

    def __init__(self, stream: FakeStream):
        self.parts, self.model, self.usage = list(stream.parts), stream.model, stream.usage
        self.finish_reason, self.fail = stream.finish_reason, stream.fail

    async def __aiter__(self):
        for part in self.parts:
            await asyncio.sleep(0)
            yield part
        await asyncio.sleep(0)
        if self.fail:
            raise self.fail


def _asyncified(kwargs: dict) -> dict:
    """The scenario's sync stream factories, wrapped so each returns the async twin of the stream it would have
    returned."""
    out = dict(kwargs)
    for key in ("llm_stream", "escalation_stream"):
        if key in out:
            out[key] = (lambda sync_factory: lambda prompt: AsyncReplay(sync_factory(prompt)))(out[key])
    return out


def async_run(question: str, build, *, offload: bool) -> dict:
    """The recording's own normalisation (``json`` round trip, sets sorted) applied to the async twin's events."""
    prompts: list[str] = []

    async def go():
        extra = {"limiters": make_limiters(_Settings())} if offload else {}
        async with aclosing(answerer_async.astream_answer_for_context(
                question, _retrieval(), "hybrid", **_asyncified(build(prompts)), **extra)) as events:
            return [event async for event in events]

    async def guarded():
        with anyio.fail_after(20):
            return await go()

    events = asyncio.run(guarded())
    return json.loads(json.dumps({"events": events, "prompts": prompts}, default=sorted))


class _Settings:
    embed_slots, db_thread_limit = 1, 4


@pytest.mark.parametrize("offload", [False, True], ids=["checks_inline", "checks_on_worker_threads"])
@pytest.mark.parametrize("name", NAMES)
def test_the_async_twin_replays_the_pre_m4_recording_event_for_event(name, offload):
    recorded = json.loads(RECORDING.read_text(encoding="utf-8"))
    question, build = next((q, b) for n, q, b in _scenarios() if n == name)
    assert async_run(question, build, offload=offload) == recorded[name]


@pytest.mark.parametrize("name", NAMES)
def test_the_async_twin_and_the_sync_writer_serialise_to_the_same_bytes(name):
    """``dict ==`` ignores key order and the recording is stored with sorted keys, so compare the serialisation in
    emission order too: this is what a client would receive."""
    question, build = next((q, b) for n, q, b in _scenarios() if n == name)

    def wire(run: dict) -> str:
        return json.dumps(run["events"])

    assert wire(async_run(question, build, offload=False)) == wire(sync_run(question, build))


def test_every_recorded_scenario_is_replayed_and_asks_for_the_same_prompts():
    recorded = json.loads(RECORDING.read_text(encoding="utf-8"))
    assert set(NAMES) == set(recorded)
    for name, question, build in _scenarios():
        replay = async_run(question, build, offload=False)
        assert replay["prompts"] == recorded[name]["prompts"]

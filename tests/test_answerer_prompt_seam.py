"""M4 step 0 pin: the SEC writer's events are byte-for-byte what they were before M4 (docs/v2/M4_PLAN.md section 4.3).

M4 extracts the tail of ``stream_answer_for_context`` (everything after the prompt is rendered) into a reusable function so the
upload workspace can reuse the verifier, the router and the escalation with its OWN template. That refactor must not change a
single SEC event: ``tests/data/answer_events_pre_m4.json`` was recorded from the pre-refactor code (commit cff2415) by running this
module as a script, and every scenario below must reproduce it exactly, including a hash of every prompt a model received.

Re-record ONLY for a deliberate change to the SEC answer path (then say so in the commit):
    PYTHONPATH=src .venv/Scripts/python tests/test_answerer_prompt_seam.py --record
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from semigraph.retrieval import answerer

RECORDING = Path(__file__).parent / "data" / "answer_events_pre_m4.json"
CHEAP, STRONG = "openai/gpt-6-luna", "anthropic/claude-sonnet-5"
C1, C2 = "0001045810-25-000023:I.1A:0003", "0001045810-25-000023:I.1:0001"


class FakeStream:
    """A scripted model stream: yields ``parts`` (raising ``fail`` after them when set) and carries usage / model / finish."""

    def __init__(self, parts, *, model, usage=None, finish_reason="stop", fail=None):
        self.parts, self.model, self.usage, self.finish_reason, self.fail = list(parts), model, usage, finish_reason, fail

    def __iter__(self):
        yield from self.parts
        if self.fail:
            raise self.fail


def _retrieval() -> dict:
    return {"anchors": {"NVDA": "0001045810"}, "anchor_defaulted": False,
            "edges": [{"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC", "status": "Active", "chunk_ids": [C2]}],
            "metrics": [], "temporal": [], "temporal_pairs": [], "temporal_passages": [],
            "risks": [{"company": "Nvidia", "category": "Supply chain", "summary": "Relies on TSMC for wafers.", "chunk_id": C1}],
            "chunks": [{"chunk_id": C1, "text": "We depend on TSMC to manufacture our GPUs; revenue was $60.9 billion."},
                       {"chunk_id": C2, "text": "Nvidia designs GPUs and relies on foundry partners."}]}


def _factory(prompts: list[str], *streams: FakeStream):
    """callable(prompt) -> the next scripted stream, remembering a hash of each prompt it was given."""
    queue = list(streams)

    def call(prompt: str) -> FakeStream:
        prompts.append(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16])
        return queue.pop(0)
    return call


USAGE = {"prompt_tokens": 1200, "completion_tokens": 40}
GOOD = [f"Nvidia depends on TSMC [{C1}]", f" and on foundry partners [{C2}]."]
BAD = ["Nvidia depends on Samsung [0000000000-00-000000:I.1:0001]."]


def _scenarios():
    """(name, question, kwargs builder) - each builder gets the prompt log and returns stream_answer_for_context kwargs."""
    q, q_change = "Who makes Nvidia's GPUs?", "How have Nvidia's supply-chain risk factors changed over time?"
    return [
        ("live", q, lambda log: {"llm_stream": _factory(log, FakeStream(GOOD, model=CHEAP, usage=USAGE))}),
        ("draft_clean", q, lambda log: {
            "llm_stream": _factory(log, FakeStream(GOOD, model=CHEAP, usage=USAGE)),
            "escalation_model": STRONG, "model": CHEAP}),
        ("draft_rejected", q, lambda log: {
            "llm_stream": _factory(log, FakeStream(BAD, model=CHEAP, usage=USAGE)),
            "escalation_stream": _factory(log, FakeStream(GOOD, model=STRONG, usage=USAGE)),
            "escalation_model": STRONG, "model": CHEAP}),
        ("routed_strong", q_change, lambda log: {
            "llm_stream": _factory(log),
            "escalation_stream": _factory(log, FakeStream(GOOD, model=STRONG, usage=USAGE)),
            "escalation_model": STRONG, "model": CHEAP}),
        ("live_error", q, lambda log: {
            "llm_stream": _factory(log, FakeStream(GOOD[:1], model=CHEAP, usage=USAGE, fail=RuntimeError("provider reset")))}),
        ("draft_error", q, lambda log: {
            "llm_stream": _factory(log, FakeStream([], model=CHEAP, fail=TimeoutError("draft timed out"))),
            "escalation_stream": _factory(log, FakeStream(GOOD, model=STRONG, usage=USAGE)),
            "escalation_model": STRONG, "model": CHEAP}),
    ]


def _run(question: str, build) -> dict:
    prompts: list[str] = []
    events = list(answerer.stream_answer_for_context(question, _retrieval(), "hybrid", **build(prompts)))
    return json.loads(json.dumps({"events": events, "prompts": prompts}, default=sorted))


def record() -> dict:
    return {name: _run(question, build) for name, question, build in _scenarios()}


@pytest.mark.parametrize("name", [s[0] for s in _scenarios()])
def test_sec_writer_events_are_unchanged_since_before_m4(name):
    recorded = json.loads(RECORDING.read_text(encoding="utf-8"))
    question, build = next((q, b) for n, q, b in _scenarios() if n == name)
    assert _run(question, build) == recorded[name]


DOC = "doc:0123456789ab:v1:0003"


def test_an_uploaded_document_excerpt_is_a_grounding_source_like_a_filing_excerpt():
    context = f"ACTIVE RISKS:\n- x [{C1}]\n\nEXCERPTS:\n[{C2}]\nNvidia text.\n\n[{DOC}]\nOur margin was 41.5% in Q2.\n"
    sources = answerer.sources_from_context(context)
    assert sources[DOC].strip() == "Our margin was 41.5% in Q2." and "Nvidia text." in sources[C2]


def test_the_excerpt_line_change_does_not_touch_the_template_fingerprint():
    assert answerer.template_fingerprint() == "4d0a62f5a0"


def _prompt_run(streams: dict, **kwargs) -> list[dict]:
    prompts: list[str] = []
    built = {k: _factory(prompts, *v) for k, v in streams.items()}
    return list(answerer.stream_answer_for_prompt(
        "What does my document say about margins?", "CUSTOM PROMPT", f"EXCERPTS:\n[{DOC}]\nOur margin was 41.5% in Q2.\n",
        {DOC}, [DOC], "hybrid", sources={DOC: "Our margin was 41.5% in Q2."}, **built, **kwargs))


LINKED = [f"Margin was 41.5% [{DOC}]. See ![x](https://evil.test/p.png) and [here](https://evil.test)."]


def _strip(text: str) -> str:
    return text.replace(" See ![x](https://evil.test/p.png) and [here](https://evil.test).", "")


def test_the_prompt_tail_emits_no_retrieval_event_and_verifies_with_the_callers_ids():
    events = _prompt_run({"llm_stream": [FakeStream([f"Margin was 41.5% [{DOC}]."], model=CHEAP, usage=USAGE)]})
    assert [e["event"] for e in events] == ["delta", "done"]
    assert events[-1]["citations"] == [DOC] and events[-1]["checks"]["citations_retrieved"] is True
    assert events[-1]["checks"]["numbers_grounded"] is True


def test_force_buffered_releases_one_postprocessed_delta_even_without_an_escalation_model():
    parts = [f"Margin was 41.5% [{DOC}].", " See ![x](https://evil.test/p.png) and [here](https://evil.test)."]
    assert "".join(parts) == LINKED[0]
    events = _prompt_run({"llm_stream": [FakeStream(parts, model=CHEAP, usage=USAGE)]},
                         postprocess=_strip, force_buffered=True)
    deltas = [e["text"] for e in events if e["event"] == "delta"]
    assert deltas == [f"Margin was 41.5% [{DOC}]."] and events[-1]["answer"] == deltas[0]


def test_force_buffered_postprocesses_the_escalated_and_the_routed_strong_answers_too():
    rejected = _prompt_run({"llm_stream": [FakeStream(["No citation here."], model=CHEAP, usage=USAGE)],
                            "escalation_stream": [FakeStream(LINKED, model=STRONG, usage=USAGE)]},
                           escalation_model=STRONG, model=CHEAP, postprocess=_strip, force_buffered=True)
    assert [e["event"] for e in rejected] == ["escalated", "delta", "done"]
    assert "evil.test" not in rejected[-1]["answer"] and rejected[-1]["escalated"] is True
    routed = list(answerer.stream_answer_for_prompt(
        "How has my document's margin changed over time?", "P", "", {DOC}, [DOC], "hybrid",
        llm_stream=_factory([]), escalation_stream=_factory([], FakeStream(LINKED, model=STRONG, usage=USAGE)),
        escalation_model=STRONG, model=CHEAP, postprocess=_strip, force_buffered=True))
    assert routed[-1]["routed"] == "strong" and "evil.test" not in routed[-1]["answer"]
    assert [e["event"] for e in routed] == ["delta", "done"]


def test_a_buffered_stream_that_fails_reports_the_postprocessed_partial_and_its_spend():
    events = _prompt_run({"llm_stream": [FakeStream(LINKED, model=CHEAP, usage=USAGE, fail=RuntimeError("reset"))]},
                         postprocess=_strip, force_buffered=True)
    assert [e["event"] for e in events] == ["error"]
    assert "evil.test" not in events[0]["partial"] and events[0]["cost_usd"] is not None


def test_postprocess_without_buffering_is_refused():
    with pytest.raises(ValueError):
        _prompt_run({"llm_stream": [FakeStream(["x"], model=CHEAP)]}, postprocess=_strip)


def test_the_recording_covers_every_path_of_the_writer():
    recorded = json.loads(RECORDING.read_text(encoding="utf-8"))
    kinds = {name: [e["event"] for e in rec["events"]] for name, rec in recorded.items()}
    assert kinds["live"] == ["retrieval", "delta", "delta", "done"]
    assert kinds["draft_clean"] == ["retrieval", "delta", "done"]
    assert kinds["draft_rejected"] == ["retrieval", "escalated", "delta", "delta", "done"]
    assert kinds["routed_strong"][-1] == "done" and recorded["routed_strong"]["events"][-1]["routed"] == "strong"
    assert kinds["live_error"][-1] == "error" and kinds["draft_error"][1] == "escalated"


if __name__ == "__main__" and "--record" in sys.argv:
    RECORDING.parent.mkdir(parents=True, exist_ok=True)
    RECORDING.write_text(json.dumps(record(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"recorded {RECORDING}")

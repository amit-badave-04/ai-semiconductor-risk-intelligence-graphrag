"""Council 10, W1 and W3 (docs/decisions, docs/specs/m5b-frontend.md I3): the upload-job fixtures are hand-built dicts
(``source: "route-recorded, job dicts constructed"``), so nothing tied them to what ``uploads/jobs.py`` really emits.

W1 (drift): ``tests/data/sse/job_ready.expected.json`` and ``job_failed.expected.json`` are compared, state by state, with
the events the real job code writes when it is driven synchronously over a fake repository (the ``_worker`` thread body
of ``test_serve_upload_jobs``): the same key sets, the nested ``progress`` and ``error`` key sets, the same value types and
the same order of states. The failed fixture is compared for equality, message included (``JOB_ERROR_MESSAGES``), because
a parse failure of the real code produces exactly its four events. Either side changing fails a test.

W3 (size): the largest frame ``jobs.py`` can emit, worst case in every field and serialised as the route does
(``event: job`` / ``data: <json>``), fits ``MAX_BUFFER_SIZE`` of ``web/src/lib/sse.ts`` with a stated margin.
"""

import inspect
import json
import re
from pathlib import Path

import pytest

from semigraph.uploads import changes, compare, jobs
from semigraph.uploads.parse import ParseError
from test_serve_upload_jobs import DOC, FakeApp, _block, _events, _parsed, _run, fake_repo  # noqa: F401 - fake_repo is a fixture

ROOT = Path(__file__).resolve().parents[1]
SSE_DIR = ROOT / "tests" / "data" / "sse"
SSE_TS = ROOT / "web" / "src" / "lib" / "sse.ts"
READY_JOB_ID = "00112233445566ff"
FAILED_JOB_ID = "ffeeddccbbaa9988"
MARGIN = 100     # the largest frame must be at least this many times smaller than the buffer cap
STRING_CAP = 200  # a generous bound for any one string field of an event (the longest fixed one is far shorter)
INT_DIGITS = 20


def fixture_events(name: str) -> list[dict]:
    return json.loads((SSE_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))["events"]


def shape(value):
    """The key sets and value types of a JSON value, not its values."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in sorted(value.items())}
    if value is None:
        return "null"
    return type(value).__name__


def shapes_by_state(events: list[dict]) -> dict[str, list]:
    by_state: dict[str, list] = {}
    for event in events:
        by_state.setdefault(event["state"], [])
        if shape(event) not in by_state[event["state"]]:
            by_state[event["state"]].append(shape(event))
    return {state: sorted(shapes, key=json.dumps) for state, shapes in by_state.items()}


def collapsed_states(events: list[dict]) -> list[str]:
    states = [event["state"] for event in events]
    return [state for i, state in enumerate(states) if i == 0 or state != states[i - 1]]


def run_real_ready(fake_repo, monkeypatch, job_id: str = READY_JOB_ID) -> list[dict]:
    parsed = _parsed([_block("Alpha bravo charlie delta echo.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    _run(FakeApp(fake_repo), job_id=job_id)
    return _events(fake_repo, job_id)


def run_real_failed(fake_repo, monkeypatch, job_id: str = FAILED_JOB_ID) -> list[dict]:
    def scanned(*args, **kwargs):
        raise ParseError("scanned", "internal detail that never reaches the client")

    monkeypatch.setattr("semigraph.uploads.parse.parse_document", scanned)
    _run(FakeApp(fake_repo), job_id=job_id)
    return _events(fake_repo, job_id)


# ---------------------------------------------------------------- W1: the fixtures against the real builders


def test_the_ready_fixture_has_the_states_in_the_order_the_real_job_emits_them(fake_repo, monkeypatch):
    real = run_real_ready(fake_repo, monkeypatch)
    assert collapsed_states(fixture_events("job_ready")) == collapsed_states(real)
    assert collapsed_states(real)[-1] == "ready"


def test_every_ready_fixture_event_has_the_key_sets_and_value_types_of_the_real_event_of_its_state(fake_repo, monkeypatch):
    real = shapes_by_state(run_real_ready(fake_repo, monkeypatch))
    assert shapes_by_state(fixture_events("job_ready")) == real


def test_the_embedding_progress_of_the_fixture_has_the_nested_keys_and_types_the_real_job_emits(fake_repo, monkeypatch):
    real = [e for e in run_real_ready(fake_repo, monkeypatch) if e["state"] == "embedding"]
    fixture = [e for e in fixture_events("job_ready") if e["state"] == "embedding"]
    assert real and fixture
    assert {json.dumps(shape(e["progress"]), sort_keys=True) for e in fixture} == {json.dumps(shape(e["progress"]), sort_keys=True) for e in real}
    assert all(set(e["progress"]) == {"done", "total", "eta_s"} for e in real + fixture)


def test_the_failed_fixture_equals_the_events_of_a_real_parse_failure_message_included(fake_repo, monkeypatch):
    real = run_real_failed(fake_repo, monkeypatch)
    assert real == fixture_events("job_failed")
    assert real[-1]["error"]["message"] == jobs.JOB_ERROR_MESSAGES["scanned"]


def test_the_failed_fixture_job_id_and_document_id_are_the_ones_of_the_real_run():
    events = fixture_events("job_failed")
    assert {e["job_id"] for e in events} == {FAILED_JOB_ID}
    assert {e["document_id"] for e in events} == {DOC}


def test_the_drift_check_notices_a_changed_key_on_either_side(fake_repo, monkeypatch):
    """The comparison itself is not vacuous: a key added to a real event, or removed from a fixture event, is a difference."""
    real = run_real_ready(fake_repo, monkeypatch)
    fixture = fixture_events("job_ready")
    drifted_real = [dict(e, extra_field=1) if e["state"] == "ready" else e for e in real]
    drifted_fixture = [{k: v for k, v in e.items() if k != "units"} if e["state"] == "ready" else e for e in fixture]
    assert shapes_by_state(drifted_real) != shapes_by_state(fixture)
    assert shapes_by_state(drifted_fixture) != shapes_by_state(real)


# ---------------------------------------------------------------- W3: the largest frame fits the buffer


def max_buffer_size() -> int:
    match = re.search(r"export const MAX_BUFFER_SIZE\s*=\s*(\d+)\s*\*\s*(\d+)\s*;", SSE_TS.read_text(encoding="utf-8"))
    assert match, "MAX_BUFFER_SIZE is no longer a product of two integers in sse.ts: update this test with it"
    return int(match.group(1)) * int(match.group(2))


def not_compared_reasons() -> list[str]:
    """Every ``not_compared_reason`` jobs.py can put in a ``ready`` event: the guards of changes.py, the failures of compare.py
    and the first-version case of jobs.py (found in the source, and counted, so a silent loss of the scan fails)."""
    guard = inspect.getsource(changes._guard_reason)
    reasons = set(re.findall(r'return "([a-z_]+)"', guard))
    reasons |= set(re.findall(r'_not_compared\("([a-z_]+)"', inspect.getsource(compare)))
    reasons |= set(re.findall(r'"not_compared_reason": "([a-z_]+)"', inspect.getsource(jobs)))
    assert {"first_version", "identical_content", "comparison_timeout", "comparison_failed"} <= reasons
    return sorted(reasons)


def frame_of(job: dict) -> str:
    """The wire frame of one job event, as ``workspace_routes._job_sse`` builds it (sse_starlette, LF separators)."""
    return f"event: job\ndata: {json.dumps(job, default=str)}\n\n"


def worst_case_events() -> list[dict]:
    big = 10 ** INT_DIGITS - 1
    base = {"job_id": "f" * 16, "document_id": "f" * 12, "version": big}
    longest_reason = max(not_compared_reasons(), key=len)
    pad = "x" * STRING_CAP
    events = [
        dict(base, state="validating"),
        dict(base, state="embedding", progress={"done": big, "total": big, "eta_s": float(f"{big}.9")}),
        dict(base, state="ready", chunks=big, units=big, items_compared=False, not_compared_reason=longest_reason, suspicious=False),
        dict(base, state="ready", chunks=big, units=big, items_compared=True, not_compared_reason=pad, suspicious=False),
    ]
    for code, message in jobs.JOB_ERROR_MESSAGES.items():
        events.append(dict(base, state="failed", error={"code": code, "message": message}))
    events.append(dict(base, state="failed", error={"code": pad, "message": pad}))
    return events


def test_the_worst_case_events_have_the_shapes_the_real_job_emits(fake_repo, monkeypatch):
    """The inflated events are built from the real key sets, so a new field in jobs.py cannot slip past the size test."""
    real_ready = shapes_by_state(run_real_ready(fake_repo, monkeypatch))
    real_failed = shapes_by_state(run_real_failed(fake_repo, monkeypatch, job_id="a" * 16))
    worst = shapes_by_state(worst_case_events())
    for state in ("validating", "embedding", "ready"):
        assert set(map(json.dumps, real_ready[state])) <= set(map(json.dumps, worst[state])) or state == "embedding"
    assert set(worst["failed"][0]) == set(real_failed["failed"][0])
    assert set(worst["ready"][0]) == set(real_ready["ready"][0])


def test_the_largest_job_frame_fits_max_buffer_size_with_a_wide_margin():
    cap = max_buffer_size()
    sizes = {len(frame_of(event).encode("utf-8")) for event in worst_case_events()}
    largest = max(sizes)
    assert largest * MARGIN < cap, f"largest job frame {largest} bytes against a cap of {cap}"


def test_the_longest_real_failure_message_is_far_below_the_string_cap():
    assert max(len(message) for message in jobs.JOB_ERROR_MESSAGES.values()) < STRING_CAP


def test_the_size_check_fails_when_the_cap_is_too_small():
    """Not vacuous: the same arithmetic with a cap of the largest frame itself does not pass."""
    largest = max(len(frame_of(event).encode("utf-8")) for event in worst_case_events())
    assert not (largest * MARGIN < largest)


@pytest.mark.parametrize("name", ["job_ready", "job_failed"])
def test_every_fixture_frame_is_far_below_the_cap(name):
    cap = max_buffer_size()
    assert max(len(frame_of(e)) for e in fixture_events(name)) * MARGIN < cap

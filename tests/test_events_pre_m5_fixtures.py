"""M5a step 0 pin: the SYNC agent and workspace event streams, recorded before the async rewrite (increment I2).

``tests/data/agent_events_pre_m5.json`` and ``tests/data/workspace_events_pre_m5.json`` were recorded from the unmodified sync code
(``_about.commit``) by ``tests/data/record_events_pre_m5.py``, which drives the real ``agent_answer_stream`` and
``stream_workspace_answer`` with scripted fakes (no Neo4j, no network, no model). This file proves two things:

1. TODAY's sync code still reproduces every scenario event-for-event, with the same totals, prompt hashes and logs, so the fixtures
   are reproducible and can be trusted as the oracle the async twin is compared against;
2. the fixtures are not trivial: every scenario streams text and ends in ``done`` or ``error``, EXCEPT the scenarios the consumer
   closed early (``inputs.close_after_events``: a client disconnect), which by design have no terminal event and report the
   abandonment instead (a WARNING with the planner's dollars and a final ``tracer.flush()``).

Replays are driven from the committed ``inputs`` alone, so the files are self-contained. Re-record ONLY for a deliberate change to
either answer path (then say so in the commit):
    uv run python tests/data/record_events_pre_m5.py
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import socket
from pathlib import Path

import pytest
from agent_fakes import LUNA, SONNET

from semigraph.retrieval.answerer import usage_cost

DATA = Path(__file__).parent / "data"
RECORDER_PATH = DATA / "record_events_pre_m5.py"


def _load_recorder():
    """The recorder is a script in ``tests/data`` (not a package, not collected by pytest): load it by path, under its own name."""
    spec = importlib.util.spec_from_file_location("record_events_pre_m5_under_test", RECORDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recorder = _load_recorder()
FILES = {"agent": recorder.AGENT_FILE, "workspace": recorder.WORKSPACE_FILE}
RUNNERS = {"agent": recorder.run_agent_scenario, "workspace": recorder.run_workspace_scenario}
DOCUMENTS = {"agent": recorder.agent_document, "workspace": recorder.workspace_document}
COMMITTED = {kind: json.loads(path.read_text(encoding="utf-8")) for kind, path in FILES.items()}
SCENARIOS = {s["name"]: (kind, s) for kind, doc in COMMITTED.items() for s in doc["scenarios"]}
NAMES = list(SCENARIOS)
TERMINAL = {"done", "error"}

EXPECTED_KINDS = {
    "agent_multi_step_live": ["step", "step", "step", "retrieval", "delta", "delta", "done"],
    "agent_draft_rejected_escalates": ["step", "retrieval", "escalated", "delta", "delta", "done"],
    "agent_refused_tool_call": ["step", "retrieval", "delta", "delta", "done"],
    "agent_planner_error_fallback": ["retrieval", "delta", "delta", "done"],
    "agent_writer_error": ["step", "retrieval", "delta", "error"],
    "agent_disconnect_mid_plan": ["step"],
    "agent_disconnect_mid_answer": ["step", "retrieval", "delta"],
    "workspace_cited_answer": ["retrieval", "delta", "done"],
    "workspace_no_evidence_refusal": ["retrieval", "delta", "done"],
    "workspace_draft_rejected_escalates": ["retrieval", "escalated", "delta", "done"],
    "workspace_as_of_stale_and_suspicious": ["retrieval", "delta", "done"],
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Scenario replay opens no socket and resolves no host. Every attempt is LOGGED (the agent's broad ``except Exception`` handlers
    could swallow the error the guard raises) and the list must be empty when the test ends. Imports are not guarded: LiteLLM
    may read its cost map when it is first imported, which happens at collection."""
    attempts: list[str] = []

    def refuse(name):
        def blocked(*args, **kwargs):
            attempts.append(name)
            raise OSError(f"{name}: a fixture replay must not touch the network")
        return blocked

    for target, attribute in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "getaddrinfo")):
        monkeypatch.setattr(target, attribute, refuse(f"{target.__name__}.{attribute}"))
    yield
    assert attempts == [], f"network access during a replay: {attempts}"


def assert_events_equal(actual: list[dict], expected: list[dict]) -> None:
    assert [e["event"] for e in actual] == [e["event"] for e in expected]
    for index, (got, want) in enumerate(zip(actual, expected, strict=True)):
        assert got == want, f"event {index} ({want['event']}) differs from the recording"


# --- 1. today's sync code reproduces the recording ------------------------------------------------------------------------------

@pytest.mark.parametrize("name", NAMES)
def test_the_sync_code_still_emits_every_recorded_event_and_total(name):
    kind, recorded = SCENARIOS[name]
    replay = RUNNERS[kind](recorded["inputs"])
    assert_events_equal(replay["events"], recorded["events"])
    assert replay["final"] == recorded["final"]
    assert replay["observed"] == recorded["observed"]


@pytest.mark.parametrize("kind", list(FILES))
def test_each_committed_file_is_exactly_what_the_recorder_writes(kind):
    assert recorder.render(DOCUMENTS[kind]()) == FILES[kind].read_text(encoding="utf-8")


def test_the_only_normalised_value_is_the_planning_time_and_it_is_replaced_by_the_placeholder():
    normalised = recorder.normalise_event({"event": "done", "agent": {"elapsed_s": 0.42, "model_calls": 2}})
    assert normalised["agent"] == {"elapsed_s": recorder.ELAPSED_PLACEHOLDER, "model_calls": 2}
    assert recorder.normalise_event({"event": "delta", "text": "x"}) == {"event": "delta", "text": "x"}
    with pytest.raises(TypeError):
        recorder.normalise_event({"event": "done", "agent": {"elapsed_s": "0.42"}})
    for kind, doc in COMMITTED.items():
        elapsed = re.findall(r'"elapsed_s": ([^,}\n]+)', json.dumps(doc))
        assert set(elapsed) <= {json.dumps(recorder.ELAPSED_PLACEHOLDER)}, (kind, elapsed)
    assert re.search(r'"elapsed_s"', json.dumps(COMMITTED["agent"]))


def test_a_scenario_with_an_escalation_model_must_name_the_cheap_model_so_no_settings_file_is_read():
    with pytest.raises(ValueError):
        recorder._escalation_kwargs({"escalation": {"model": SONNET, "stream": {}}, "model": None}, [])
    assert recorder._escalation_kwargs({"escalation": None, "model": None}, []) == {}
    for _, scenario in SCENARIOS.values():
        if scenario["inputs"]["escalation"]:
            assert scenario["inputs"]["model"] == LUNA


# --- 2. the fixtures are not trivial ----------------------------------------------------------------------------------------------

def test_there_are_enough_scenarios_and_each_has_the_documented_shape():
    assert len(SCENARIOS) >= 3 and len(SCENARIOS) == sum(len(d["scenarios"]) for d in COMMITTED.values())
    for kind, doc in COMMITTED.items():
        assert doc["scenarios"], kind
        for scenario in doc["scenarios"]:
            assert list(scenario) == ["name", "inputs", "events", "final", "observed"]
            assert scenario["events"], scenario["name"]


@pytest.mark.parametrize("name", NAMES)
def test_a_scenario_streams_text_and_ends_in_a_terminal_event_unless_the_consumer_closed_it(name):
    _, scenario = SCENARIOS[name]
    kinds = [e["event"] for e in scenario["events"]]
    assert TERMINAL.isdisjoint(kinds[:-1]), "a terminal event is only ever the last event"
    if scenario["inputs"].get("close_after_events") is not None:
        assert len(kinds) == scenario["inputs"]["close_after_events"] and TERMINAL.isdisjoint(kinds)
        assert scenario["final"]["terminal"] is None and scenario["final"]["abandoned"] is True
        return
    assert "delta" in kinds and kinds[-1] in TERMINAL
    assert scenario["final"]["terminal"] == kinds[-1] and scenario["final"]["abandoned"] is False


def test_the_recording_covers_every_path_it_names():
    assert {name: [e["event"] for e in SCENARIOS[name][1]["events"]] for name in EXPECTED_KINDS} == EXPECTED_KINDS
    assert set(EXPECTED_KINDS) == set(SCENARIOS)
    assert {kind for kind, _ in SCENARIOS.values()} == {"agent", "workspace"}


def test_the_about_block_names_the_commit_the_date_the_command_and_the_normalisation():
    for kind, doc in COMMITTED.items():
        about = doc["_about"]
        assert re.fullmatch(r"[0-9a-f]{7,40}", about["commit"]), kind
        assert about["recorded"] == "2026-10-03" and about["regenerate"] == "uv run python tests/data/record_events_pre_m5.py"
        assert recorder.ELAPSED_PLACEHOLDER in about["normalised"]["done.agent.elapsed_s"]
        assert recorder.DELIMITER_PLACEHOLDER in about["normalised"]["workspace prompt"]


def test_no_volatile_value_is_committed():
    for kind, path in FILES.items():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text), f"{kind}: a uuid"
        assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text), f"{kind}: a timestamp"
        assert not re.search(r"\b[A-Za-z]:[\\/](?!/)|/Users/|/home/", text), f"{kind}: an absolute path"
        delimiters = set(re.findall(r"<<<DOC-[A-Z]{12}>>>", text))
        assert delimiters <= {recorder.DELIMITER_PLACEHOLDER}, f"{kind}: a raw workspace delimiter {delimiters}"


# --- the recorded behaviour of each path (what the async twin must keep) ----------------------------------------------------------

def _planner_usd(inputs: dict, turns: int | None = None) -> float:
    spent = inputs["planner"][:turns]
    return usage_cost({"prompt_tokens": sum(t["usage"][0] for t in spent), "completion_tokens": sum(t["usage"][1] for t in spent)}, LUNA)


def _writer_usd(spec: dict) -> float:
    return usage_cost({"prompt_tokens": spec["usage"][0], "completion_tokens": spec["usage"][1]}, spec["model"])


def test_a_refused_tool_call_is_a_failed_step_and_the_run_is_not_a_fallback():
    _, scenario = SCENARIOS["agent_refused_tool_call"]
    step, done = scenario["events"][0], scenario["events"][-1]
    assert step["tool"] == "run_cypher" and step["ok"] is False
    assert done["agent"]["fallback_reason"] is None and done["agent"]["tool_calls"] == [{"tool": "run_cypher", "args": {}, "ok": False}]


def test_a_planner_error_falls_back_to_the_plain_prefetch_and_says_so():
    _, scenario = SCENARIOS["agent_planner_error_fallback"]
    done = scenario["events"][-1]
    assert done["agent"]["fallback_reason"] == "planner_error:RuntimeError" and done["agent"]["planner_cost_usd"] == 0.0
    assert [e["event"] for e in scenario["events"]][0] == "retrieval"
    assert any("the planner call failed" in w for w in scenario["observed"]["warnings"])


def test_the_prompts_show_that_only_a_successful_tool_changes_what_the_writer_sees():
    hashes = {name: SCENARIOS[name][1]["observed"]["prompt_hashes"] for name in SCENARIOS if name.startswith("agent_")}
    plain = hashes["agent_refused_tool_call"]
    assert len(plain) == 1 and hashes["agent_planner_error_fallback"] == plain and hashes["agent_writer_error"] == plain
    assert hashes["agent_draft_rejected_escalates"] == plain * 2          # the strong model gets the byte-identical prompt
    assert hashes["agent_multi_step_live"] and hashes["agent_multi_step_live"] != plain


def test_a_writer_error_carries_the_partial_and_the_planners_dollars_folded_into_the_cost():
    _, scenario = SCENARIOS["agent_writer_error"]
    error, inputs = scenario["events"][-1], scenario["inputs"]
    assert error["event"] == "error" and "stream interrupted" in error["detail"] and error["partial"] == "".join(inputs["writer"]["parts"])
    assert error["cost_usd"] == pytest.approx(_planner_usd(inputs) + _writer_usd(inputs["writer"]))
    assert scenario["final"]["cost_usd"] == error["cost_usd"] and scenario["final"]["usage"] == error["usage"]


def test_an_escalated_agent_answer_prices_the_planner_the_draft_and_the_strong_model_each_at_their_own_rates():
    _, scenario = SCENARIOS["agent_draft_rejected_escalates"]
    done, inputs = scenario["events"][-1], scenario["inputs"]
    assert done["escalated"] is True and done["answered_by"] == SONNET and done["agent"]["planner_cost_usd"] == pytest.approx(_planner_usd(inputs))
    expected = _planner_usd(inputs) + _writer_usd(inputs["writer"]) + _writer_usd(inputs["escalation"]["stream"])
    assert done["cost_usd"] == pytest.approx(expected) and scenario["final"]["cost_usd"] == done["cost_usd"]


@pytest.mark.parametrize("name,planner_turns_done", [("agent_disconnect_mid_plan", 1), ("agent_disconnect_mid_answer", 2)])
def test_a_closed_agent_stream_reports_only_the_planners_spend_through_a_warning_and_a_final_flush(name, planner_turns_done):
    _, scenario = SCENARIOS[name]
    spend = _planner_usd(scenario["inputs"], planner_turns_done)
    assert scenario["final"]["abandoned_spend_usd"] == pytest.approx(spend)
    assert scenario["final"]["usage"] is None and scenario["final"]["cost_usd"] is None
    assert [w for w in scenario["observed"]["warnings"] if "abandoned" in w] == [
        f"semigraph.agent: the agent stream was abandoned before a terminal event; the planner had already cost ${spend:.6f}"]
    assert scenario["observed"]["tracer_calls"][-1] == ["flush", None]


def test_the_agent_file_says_what_the_sync_code_does_and_does_not_report_on_abandonment():
    notes = COMMITTED["agent"]["notes"]
    assert "no usage or cost event is yielded" in notes["abandonment"] and "close_after_events" in notes["early_close"]


def test_a_workspace_answer_is_one_stripped_delta_and_its_done_carries_the_workspace_block():
    _, cited = SCENARIOS["workspace_cited_answer"]
    delta, done = cited["events"][1], cited["events"][-1]
    assert "evil.test" not in delta["text"] and "![" not in delta["text"] and delta["text"] == done["answer"]
    assert done["citations"] == sorted(done["citations"]) and set(done["workspace"]) == {
        "id_hash", "doc_chunks", "stale_citations", "suspicious"}
    assert cited["events"][0]["doc_chunks"] == 2 and done["workspace"]["id_hash"] != cited["inputs"]["workspace_id"]
    assert cited["observed"]["upload_queries"] == [["search_current", 6], ["chunk_texts", done["citations"]]]


def test_a_workspace_with_no_evidence_gets_a_refusal_with_no_citation():
    _, scenario = SCENARIOS["workspace_no_evidence_refusal"]
    done = scenario["events"][-1]
    assert scenario["events"][0]["doc_chunks"] == 0 and done["citations"] == [] and done["checks"]["is_refusal"] is True
    assert done["workspace"]["doc_chunks"] == 0 and scenario["observed"]["upload_queries"] == [["search_current", 6]]


def test_a_rejected_workspace_draft_escalates_and_the_strong_answer_is_buffered_too():
    _, scenario = SCENARIOS["workspace_draft_rejected_escalates"]
    escalated, done, inputs = scenario["events"][1], scenario["events"][-1], scenario["inputs"]
    assert escalated["event"] == "escalated" and escalated["to"] == SONNET and escalated["reasons"]
    assert done["escalated"] is True and done["answered_by"] == SONNET and done["citations"] == [recorder.DOC_A]
    expected = _writer_usd(inputs["writer"]) + _writer_usd(inputs["escalation"]["stream"])
    assert done["cost_usd"] == pytest.approx(expected) and len(scenario["observed"]["prompt_hashes"]) == 2


def test_an_as_of_workspace_ask_reports_a_stale_citation_and_flags_the_injection_shaped_text():
    _, scenario = SCENARIOS["workspace_as_of_stale_and_suspicious"]
    done = scenario["events"][-1]
    assert done["workspace"]["stale_citations"] == [recorder.DOC_OLD] and done["workspace"]["suspicious"] is True
    assert scenario["observed"]["upload_queries"][0] == ["search_as_of", 6]


# --- 3. nothing here needs Neo4j or the network ----------------------------------------------------------------------------------

def _imported_modules(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_recorder_and_this_test_import_no_driver_no_http_client_and_no_serving_code():
    forbidden_roots = {"neo4j", "requests", "httpx", "urllib3", "aiohttp", "litellm", "langfuse", "fastapi"}
    for path in (RECORDER_PATH, Path(__file__)):
        modules = _imported_modules(path)
        assert not {m.split(".")[0] for m in modules} & forbidden_roots, path.name
        assert not [m for m in modules if m.startswith(("semigraph.graph", "semigraph.serve"))], path.name

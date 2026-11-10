"""What the fixture server answers to the browser flows of I3 (docs/specs/m5b-frontend.md S13, I3 main-first item 7).

``tests/e2e/fixture_replay.py`` makes ``/api/ask`` replay the recorded byte fixtures of ``tests/data/sse`` keyed by the question
text, serves the workspace routes (create, upload, job stream, changes, evidence, delete) over a small in-memory fake, and
answers the evidence of the ids those fixtures cite. Everything runs under the guards of S13: no ``Settings()``, no LLM, no
Neo4j. The fixtures are the SAME files the browser tests parse, so a flow cannot pass on invented data.
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from serve_state_fakes import fresh_drain  # noqa: F401 - a fixture

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
import fixture_replay as replay  # noqa: E402
import serve_fixture_app as fixture_app  # noqa: E402

from semigraph.serve import routes  # noqa: E402

pytestmark = pytest.mark.usefixtures("fresh_drain")

SSE = Path(__file__).parent / "data" / "sse"
INDEX_HTML = '<!doctype html><html><head><title>dist</title></head><body><div id="root"></div></body></html>'


def recorded(name: str) -> list[dict]:
    return json.loads((SSE / f"{name}.expected.json").read_text(encoding="utf-8"))["events"]


def sse_events(text: str) -> list[dict]:
    """The JSON of every ``data:`` line of an event-stream body."""
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


@pytest.fixture(autouse=True)
def instant_jobs(monkeypatch):
    monkeypatch.setattr(replay, "JOB_STEP_DELAY_S", 0)


@pytest.fixture
def dist(tmp_path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    return root


@pytest.fixture
def guards():
    installed = fixture_app.install_guards()
    try:
        yield installed
    finally:
        installed.undo()


@pytest.fixture
def make_client(guards, dist):
    # The app's patches (the stubbed agent module among them) live in a MonkeyPatch of their own that is undone BEFORE the
    # guards are: the shared ``monkeypatch`` is set up first (instant_jobs asks for it), so it would unwind last and put the
    # stub back after the guards were undone, leaving the real module unscanned and bound to the refuser.
    mp = pytest.MonkeyPatch()

    def make(**kw):
        return TestClient(fixture_app.build_fixture_app(dist, mp=mp, **kw))

    try:
        yield make
    finally:
        mp.undo()


@pytest.fixture
def client(make_client):
    with make_client(ui_v2_preview=True, agent_enabled=True) as c:
        yield c


def ask(client, question, strategy="hybrid", **extra):
    return client.post("/api/ask", json={"question": question, "strategy": strategy, **extra}, headers=extra.pop("headers", {}))


def events_of(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return sse_events(response.text)


# ---------------------------------------------------------------- /api/ask replays the recorded fixtures

def test_a_cached_example_replays_the_recorded_cached_done_with_no_live_event(client):
    events = events_of(ask(client, replay.cached_question()))
    assert [e["event"] for e in events] == ["done"]
    done, expected = events[0], recorded("cached_done")[0]
    assert done["cached"] is True and done["answer"] == expected["answer"] and done["citations"] == expected["citations"]
    assert "usage" not in done


def test_the_cached_question_is_one_of_the_listed_examples(client):
    listed = [e["question"] for e in client.get("/api/examples").json()["examples"]]
    assert replay.cached_question() in listed


# docs/specs/m5b-redesign.md RD5: the Ask page leads with six curated starters, so the fixture server holds a saved answer for
# each (the recorded cached_done answer), and a newcomer's first click is the path the e2e specs exercise.
def test_the_fixture_starter_ids_are_the_web_app_s_curated_list():
    source = (Path(__file__).resolve().parents[1] / "web" / "src" / "lib" / "starters.ts").read_text(encoding="utf-8")
    block = source[source.index("export const STARTERS"):source.index("];", source.index("export const STARTERS"))]
    import re
    assert tuple(re.findall(r'id: "(\w+)"', block)) == replay.STARTER_IDS


@pytest.mark.parametrize("starter", replay.STARTER_IDS)
def test_every_starter_is_answered_from_the_cache(client, starter):
    question = next(e["question"] for e in client.get("/api/examples").json()["examples"] if e["id"] == starter)
    events = events_of(ask(client, question))
    assert [e["event"] for e in events] == ["done"] and events[0]["cached"] is True


@pytest.mark.parametrize("key, fixture", [("live", "live_hybrid"), ("escalated", "escalated"),
                                          ("error_before", "error_before_delta"), ("error_after", "error_after_delta")])
def test_each_question_replays_its_recorded_event_sequence(client, key, fixture):
    events = events_of(ask(client, replay.QUESTIONS[key]))
    want = recorded(fixture)
    assert [e["event"] for e in events] == [e["event"] for e in want]
    assert [e.get("text") for e in events if e["event"] == "delta"] == [e["text"] for e in want if e["event"] == "delta"]
    terminal = events[-1]
    if terminal["event"] == "done":
        assert terminal["answer"] == want[-1]["answer"]
    else:
        assert terminal["detail"] == want[-1]["detail"]


def test_the_agent_question_replays_the_steps_when_the_agent_is_on(client):
    events = events_of(ask(client, replay.QUESTIONS["agent"], strategy="agent"))
    want = recorded("agent_steps")
    assert [e["event"] for e in events] == [e["event"] for e in want]
    assert [e["summary"] for e in events if e["event"] == "step"] == [e["summary"] for e in want if e["event"] == "step"]


def test_an_unknown_question_still_gets_the_default_fake_answer(client):
    events = events_of(ask(client, "Some other question nobody recorded?"))
    assert events[-1]["event"] == "done" and events[-1]["citations"]


# ---------------------------------------------------------------- the bot check (a site-key server)

@pytest.fixture
def keyed(make_client):
    with make_client(site_key="dummy-site-key", turnstile_stub=True, ui_v2_preview=True) as c:
        yield c


def test_the_site_key_server_hands_out_its_dummy_key(keyed):
    assert keyed.get("/api/config").json() == {"turnstile_site_key": "dummy-site-key"}


def test_a_live_ask_without_a_token_is_a_403_with_the_server_message(keyed):
    r = ask(keyed, replay.QUESTIONS["live"])
    assert r.status_code == 403 and r.json()["detail"] == routes.MSG_BOT


def test_a_live_ask_with_any_token_passes_the_stubbed_check(keyed):
    events = events_of(ask(keyed, replay.QUESTIONS["live"], turnstile_token="fixture-token"))
    assert events[-1]["event"] == "done"


def test_a_cached_example_needs_no_token_even_on_the_site_key_server(keyed):
    events = events_of(ask(keyed, replay.cached_question()))
    assert events[0]["cached"] is True


def test_the_default_server_has_no_key_and_accepts_an_ask_with_no_token(client):
    assert client.get("/api/config").json() == {"turnstile_site_key": ""}
    assert events_of(ask(client, replay.QUESTIONS["live"]))[-1]["event"] == "done"


# ---------------------------------------------------------------- the workspace over the fake repo

def new_workspace(client) -> tuple[str, dict]:
    created = client.post("/api/workspace", json={})
    assert created.status_code == 201, created.text
    body = created.json()
    return body["workspace_id"], {"X-Workspace-Token": body["token"]}


def upload(client, ws, headers, filename="memo.txt", **fields):
    return client.post(f"/api/workspace/{ws}/documents", headers=headers, files={"file": (filename, b"hello world", "text/plain")},
                       data=fields)


def watch(client, ws, headers, job_id) -> list[dict]:
    return events_of(client.get(f"/api/workspace/{ws}/jobs/{job_id}", headers=headers))


def test_stats_say_uploads_are_on(client):
    assert client.get("/api/stats").json()["uploads_enabled"] is True


def test_a_workspace_can_be_created_read_and_deleted_and_is_then_gone(client):
    ws, headers = new_workspace(client)
    assert len(ws) == 32
    got = client.get(f"/api/workspace/{ws}", headers=headers)
    assert got.status_code == 200 and got.json()["workspace_id"] == ws and got.json()["documents"] == []
    assert client.delete(f"/api/workspace/{ws}", headers=headers).status_code == 204
    assert client.get(f"/api/workspace/{ws}", headers=headers).status_code == 404


def test_a_wrong_token_and_an_unknown_workspace_are_the_same_404(client):
    ws, _ = new_workspace(client)
    assert client.get(f"/api/workspace/{ws}", headers={"X-Workspace-Token": "wrong"}).status_code == 404
    assert client.get(f"/api/workspace/{'f' * 32}", headers={"X-Workspace-Token": "x"}).status_code == 404


def test_an_upload_replays_job_ready_with_the_real_job_and_document_ids(client):
    ws, headers = new_workspace(client)
    accepted = upload(client, ws, headers)
    assert accepted.status_code == 202
    job = accepted.json()
    events = watch(client, ws, headers, job["job_id"])
    want = recorded("job_ready")
    assert [e["state"] for e in events] == [e["state"] for e in want]
    assert {e["job_id"] for e in events} == {job["job_id"]} and {e["document_id"] for e in events} == {job["document_id"]}
    assert events[-1]["state"] == "ready"
    listed = client.get(f"/api/workspace/{ws}", headers=headers).json()["documents"]
    assert [d["document_id"] for d in listed] == [job["document_id"]]
    assert listed[0]["versions"][0]["version"] == 1 and listed[0]["versions"][0]["is_current"] is True


def test_a_file_whose_name_says_fail_replays_job_failed(client):
    ws, headers = new_workspace(client)
    job = upload(client, ws, headers, filename="fail-scan.txt", title="fail-scan.txt").json()
    events = watch(client, ws, headers, job["job_id"])
    assert [e["state"] for e in events] == [e["state"] for e in recorded("job_failed")]
    assert events[-1]["error"]["code"] == "scanned"


def test_the_job_stream_of_another_workspace_is_a_404(client):
    ws, headers = new_workspace(client)
    other, other_headers = new_workspace(client)
    job = upload(client, ws, headers).json()
    assert client.get(f"/api/workspace/{other}/jobs/{job['job_id']}", headers=other_headers).status_code == 404


def test_the_upload_slot_is_free_again_once_the_job_ended(client):
    ws, headers = new_workspace(client)
    first = upload(client, ws, headers).json()
    watch(client, ws, headers, first["job_id"])
    assert upload(client, ws, headers, filename="second.txt").status_code == 202


def test_a_second_version_of_a_document_gets_version_two_and_changes_are_served(client):
    ws, headers = new_workspace(client)
    first = upload(client, ws, headers).json()
    watch(client, ws, headers, first["job_id"])
    r = client.post(f"/api/workspace/{ws}/documents", headers=headers, data={"document_id": first["document_id"]},
                    files={"file": ("memo.txt", b"hello changed world", "text/plain")})
    assert r.status_code == 202 and r.json()["version"] == 2
    watch(client, ws, headers, r.json()["job_id"])
    changes = client.get(f"/api/workspace/{ws}/changes", headers=headers,
                         params={"document_id": first["document_id"], "from": 1, "to": 2})
    assert changes.status_code == 200
    body = changes.json()
    assert body["items_compared"] is True and body["changed"] and body["added"] is not None
    missing = client.get(f"/api/workspace/{ws}/changes", headers=headers, params={"document_id": "0" * 12, "from": 1, "to": 2})
    assert missing.status_code == 404


def test_the_evidence_of_a_document_chunk_is_served_to_its_workspace_only(client):
    ws, headers = new_workspace(client)
    job = upload(client, ws, headers).json()
    watch(client, ws, headers, job["job_id"])
    chunk = f"doc:{job['document_id']}:v1:0007"
    r = client.get(f"/api/workspace/{ws}/evidence/{chunk}", headers=headers)
    assert r.status_code == 200
    row = r.json()
    assert row["id"] == chunk and row["document_id"] == job["document_id"] and row["text"]
    assert client.get(f"/api/workspace/{ws}/evidence/doc:{job['document_id']}:v1:9999", headers=headers).status_code == 404
    assert client.get(f"/api/workspace/{ws}/evidence/{chunk}", headers={"X-Workspace-Token": "wrong"}).status_code == 404


def test_a_workspace_ask_replays_the_recorded_workspace_answer(client):
    ws, headers = new_workspace(client)
    events = events_of(client.post("/api/ask", headers=headers, json={"question": replay.WORKSPACE_QUESTION, "workspace_id": ws}))
    want = recorded("workspace_done")
    assert [e["event"] for e in events] == [e["event"] for e in want]
    assert events[-1]["citations"] == want[-1]["citations"]


def test_a_workspace_ask_with_a_wrong_token_is_a_404(client):
    ws, _ = new_workspace(client)
    r = client.post("/api/ask", headers={"X-Workspace-Token": "wrong"}, json={"question": replay.WORKSPACE_QUESTION, "workspace_id": ws})
    assert r.status_code == 404


# ---------------------------------------------------------------- the evidence of what the answers cite

@pytest.mark.parametrize("cited", ["0001045810-26-000021:I.1A:0001", "xbrl:1045810:revenue:2026-01-25"])
def test_the_ids_the_recorded_answers_cite_resolve_to_evidence(client, cited):
    r = client.get(f"/api/evidence/{cited}")
    assert r.status_code == 200 and r.json()["type"] in ("chunk", "xbrl")


def test_every_public_id_of_the_recorded_answers_resolves_or_is_a_known_miss(client):
    for name in ("cached_done", "live_hybrid", "escalated"):
        for event in recorded(name):
            for cited in event.get("citations", []) if event["event"] == "done" else []:
                assert client.get(f"/api/evidence/{cited}").status_code == 200, (name, cited)


def test_an_unlisted_id_is_still_a_404(client):
    assert client.get("/api/evidence/0001045810-26-000021:I.1A:9999").status_code == 404


# ---------------------------------------------------------------- the guards hold through all of it

def test_nothing_above_reached_settings_or_an_llm(guards, client):
    ws, headers = new_workspace(client)
    job = upload(client, ws, headers).json()
    watch(client, ws, headers, job["job_id"])
    events_of(ask(client, replay.QUESTIONS["escalated"]))
    events_of(ask(client, replay.QUESTIONS["agent"], strategy="agent"))
    events_of(client.post("/api/ask", headers=headers, json={"question": replay.WORKSPACE_QUESTION, "workspace_id": ws}))
    assert guards.hits == []


def test_the_stub_flag_is_a_command_line_option_off_by_default():
    assert fixture_app.parse_args(["--turnstile-stub"]).turnstile_stub is True
    assert fixture_app.parse_args([]).turnstile_stub is False

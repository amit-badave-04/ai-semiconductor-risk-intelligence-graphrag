"""The browser-flow fixtures of the fixture server (docs/specs/m5b-frontend.md S13, I3 main-first item 7).

``serve_fixture_app.build_fixture_app`` calls :func:`install` after the app is built. It makes

* ``POST /api/ask`` replay the recorded byte fixtures of ``tests/data/sse/*.expected.json``, chosen by the question text
  (:data:`QUESTIONS`), by the strategy (``agent`` replays ``agent_steps``) and by a workspace id (replays ``workspace_done``);
  the first packaged example is seeded into the answer cache, so it replays ``cached_done`` before the bot check, as in
  production;
* the workspace routes work over a small in-memory fake (create, read, upload, a job stream that replays ``job_ready`` or
  ``job_failed`` with the job's real ids, changes, evidence of ``doc:`` chunks, delete), through the REAL routers;
* the public evidence of the ids the recorded answers cite resolve to hand-written rows (a template per id form).

Nothing here constructs ``Settings`` or calls an LLM or Neo4j. Every patch goes through the ``pytest.MonkeyPatch`` the caller owns.
"""

import hashlib
import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from serve_app_factory import COMPANY_FIXTURE_TICKERS, GENEROUS, answer_events, load_fixture

from semigraph.artifacts import load_examples
from semigraph.serve import routes, store
from semigraph.serve.guard import RateLimiter
from semigraph.uploads import jobs, repo

SSE_DIR = Path(__file__).resolve().parents[1] / "data" / "sse"

# The question text that selects each recorded scenario (test inputs, not public copy). Any other question gets the default
# fake answer of serve_app_factory.
QUESTIONS = {
    "live": "Fixture question: a live hybrid answer",
    "escalated": "Fixture question: an escalated answer",
    "agent": "Fixture question: an agent answer with steps",
    "error_before": "Fixture question: an error before any text",
    "error_after": "Fixture question: an error after some text",
}
WORKSPACE_QUESTION = "Fixture question: an answer over my uploaded document"
FAIL_MARKER = "fail"                    # an upload whose title or first bytes say this replays job_failed
JOB_STEP_DELAY_S = 0.02                 # between the replayed job events (a test sets 0)
CHUNKS_PER_DOCUMENT = 20                # doc:<id>:v<n>:0001..0020 have evidence
ASK_CHUNK_PREFIX = "0001045810-26-000021:I.1A:"
ASK_CHUNK_NUMBERS = range(1, 41)        # the recorded answers cite I.1A:0001..0040
ASK_XBRL_PREFIX = "1045810:revenue:"
DOC_CHUNK_RE = re.compile(r"^doc:([0-9a-f]{12}):v([0-9]{1,3}):([0-9]{4})$")


def recorded_events(name: str) -> list[dict]:
    return json.loads((SSE_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))["events"]


def cached_question() -> str:
    """The first packaged example: the one seeded into the answer cache."""
    return load_examples()["examples"][0]["question"]


# The Ask page's six curated starters (web/src/lib/starters.ts STARTERS, pinned by tests/test_fixture_replay.py): each is seeded
# with the recorded cached answer too, so a newcomer's first click is answered from the cache, as in production.
STARTER_IDS = ("N2", "D3", "Q2", "R1", "T7", "NG15")


def cached_questions() -> list[str]:
    """Every question seeded into the answer cache: the first packaged example and the starters."""
    by_id = {e["id"]: e["question"] for e in load_examples()["examples"]}
    return [cached_question(), *(by_id[i] for i in STARTER_IDS)]


# ---------------------------------------------------------------- /api/ask

def events_for(question: str, strategy: str, workspace: bool) -> list[dict]:
    if workspace:
        return recorded_events("workspace_done")
    if strategy == "agent":
        return recorded_events("agent_steps")
    scenario = {QUESTIONS["live"]: "live_hybrid", QUESTIONS["escalated"]: "escalated",
                QUESTIONS["error_before"]: "error_before_delta", QUESTIONS["error_after"]: "error_after_delta"}.get(question)
    return recorded_events(scenario) if scenario else answer_events(question, strategy)


async def replay_stream(question, driver=None, embedder=None, strategy="hybrid", **kw):
    """The answer path of every strategy: yields the recorded events of the scenario the question selects."""
    for event in events_for(question, strategy, workspace="workspace_id" in kw):
        yield event


def seed_cache(app) -> None:
    done = {k: v for k, v in recorded_events("cached_done")[0].items() if k not in ("event", "cached")}
    for question in cached_questions():
        key = store.cache_key(question, "hybrid", getattr(app.state, "snapshot_id", ""))
        app.state.state.cache[key] = {**done, "source": "benchmark"}  # as store.seed_examples seeds the packaged examples


# ---------------------------------------------------------------- public evidence of the cited ids

CHUNK_ID_RE = re.compile(r"^[0-9-]{10,30}:[IVX]+\.[0-9A-Z]+:[0-9]{4}$")
SECTION_TITLES = {"I.1A": "Item 1A. Risk Factors", "I.3": "Item 3. Key Information"}


def _annual_filing(dossier: dict) -> dict:
    return next(f for f in dossier["filings"] if f["form"] in ("10-K", "20-F") and f["is_current"])


def company_evidence_rows() -> dict[tuple[str, str], dict]:
    """The evidence of every id a Company page builds from the three company fixtures (docs/specs/m5b-redesign.md R3): each
    figure, rule and relation passage, keyed by (id form, the id the evidence query receives)."""
    chunk = load_fixture("evidence_chunk_ask.handwritten.json")
    metric = load_fixture("evidence_xbrl.handwritten.json")
    rows: dict[tuple[str, str], dict] = {}
    for ticker in COMPANY_FIXTURE_TICKERS:
        dossier = load_fixture(f"dossier_{ticker.lower()}.handwritten.json")
        company, filing = dossier["company"], _annual_filing(dossier)
        filed = {"accession_no": filing["accession_no"], "form": filing["form"], "filing_date": filing["filing_date"]}
        for m in dossier["metrics"]:
            metric_id = f"{company['cik']}:{m['metric']}:{m['period_end']}"
            rows[("xbrl", metric_id)] = {**metric, **filed, "metric_id": metric_id, "metric": m["metric"], "concept": m["metric"],
                                         "value": m["value"], "unit": m["unit"], "period_start": m["period_start"],
                                         "period_end": m["period_end"], "company": company["name"], "cik": company["cik"]}
        for r in dossier["rules"]:
            rows[("fr", r["rule_id"])] = {"document_number": r["rule_id"], "title": r["target"], "publication_date": r["date"],
                                          "url": r["url"], "kind": r["kind"], "topics": "['fixture']", "relevant": True,
                                          "abstract": "Fixture abstract: a stand-in for the rule's summary."}
        for edge in dossier["edges"] + dossier["rules"]:
            for chunk_id in filter(CHUNK_ID_RE.match, edge["chunk_ids"]):
                section = chunk_id.split(":")[1]
                rows[("chunk", chunk_id)] = {**chunk, **filed, "chunk_id": chunk_id, "accession_no": chunk_id.split(":")[0],
                                             "filer": company["name"], "section_key": section,
                                             "section_title": SECTION_TITLES.get(section, section)}
    return rows


def public_evidence_rows(previous):
    """A ``run_cypher`` that answers the chunk and metric ids the recorded answers cite from templates, the ids of the three
    company fixtures from :func:`company_evidence_rows`, and everything else as ``previous`` did."""
    chunk = load_fixture("evidence_chunk_ask.handwritten.json")
    metric = load_fixture("evidence_xbrl.handwritten.json")
    company = company_evidence_rows()
    kinds = {routes.EVIDENCE_QUERY: "chunk", routes.XBRL_EVIDENCE_QUERY: "xbrl", routes.FR_EVIDENCE_QUERY: "fr"}

    def run_cypher(driver, query, **params):
        wanted = params.get("id", "")
        known = company.get((kinds.get(query, ""), wanted))
        if known is not None:
            return [dict(known)]
        if query is routes.EVIDENCE_QUERY and wanted.startswith(ASK_CHUNK_PREFIX) and wanted[len(ASK_CHUNK_PREFIX):].isdigit() \
                and int(wanted[len(ASK_CHUNK_PREFIX):]) in ASK_CHUNK_NUMBERS:
            return [{**chunk, "chunk_id": wanted}]
        if query is routes.XBRL_EVIDENCE_QUERY and wanted.startswith(ASK_XBRL_PREFIX):
            period_end = wanted[len(ASK_XBRL_PREFIX):]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", period_end):
                return [{**metric, "metric_id": wanted, "period_end": period_end}]
        return previous(driver, query, **params)

    return run_cypher


# ---------------------------------------------------------------- the workspace over a fake repo

class FakeWorkspaces:
    """What ``uploads.repo`` reads and writes, in memory. ``workspaces`` maps an id to its token, expiry, documents and jobs."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.workspaces: dict[str, dict] = {}

    # -- auth and lifecycle
    def authenticate(self, driver, ws, token):
        found = self.workspaces.get(ws)
        return bool(found) and bool(token) and secrets.compare_digest(found["token"], token)

    def touch(self, driver, ws):
        return None

    def create_workspace(self, driver, ttl_hours):
        ws, token = secrets.token_hex(16), secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        self.workspaces[ws] = {"token": token, "expires_at": expires_at, "documents": {}, "jobs": {}}
        return ws, token, expires_at

    def delete_workspace(self, driver, ws):
        return self.workspaces.pop(ws, None) is not None

    def get_workspace(self, driver, ws):
        found = self.workspaces.get(ws)
        if found is None:
            return None
        documents = [{"document_id": did, "title": d["title"], "latest_version": max(d["versions"], default=None),
                      "versions": [self._version_row(d, n) for n in sorted(d["versions"])]}
                     for did, d in found["documents"].items()]
        pages = sum(v["pages"] for d in documents for v in d["versions"] if v["is_current"])
        return {"workspace_id": ws, "expires_at": found["expires_at"], "documents": documents,
                "usage": {"documents": len(documents), "pages": pages, "embedded_tokens": 0}}

    @staticmethod
    def _version_row(document: dict, number: int) -> dict:
        return {"version": number, "created_at": document["versions"][number]["created_at"], "pages": 1, "chars": 11,
                "is_current": number == max(document["versions"]), "status": "ready", "items_compared": number > 1,
                "not_compared_reason": None, "suspicious": False}

    # -- reads the upload route makes
    def quota(self, driver, ws):
        docs = self.workspaces[ws]["documents"]
        return {"documents": len(docs), "versions_by_document": {d: len(v["versions"]) for d, v in docs.items()},
                "pages": len(docs), "pages_by_document": {d: 1 for d in docs}, "embedded_tokens": 0}

    def latest_version(self, driver, ws, document_id):
        document = self.workspaces[ws]["documents"].get(document_id)
        if not document or not document["versions"]:
            return None
        number = max(document["versions"])
        return {"version": number, "content_hash": document["versions"][number]["content_hash"]}

    def next_version(self, ws, document_id) -> int:
        document = self.workspaces[ws]["documents"].get(document_id)
        return max(document["versions"]) + 1 if document and document["versions"] else 1

    def record_version(self, ws, document_id, title, content_hash) -> None:
        with self.lock:
            found = self.workspaces.get(ws)
            if found is None:
                return                                  # the workspace was deleted while the job ran
            document = found["documents"].setdefault(document_id, {"title": title or "Uploaded document", "versions": {}})
            document["versions"][max(document["versions"], default=0) + 1] = {
                "content_hash": content_hash, "created_at": datetime.now(timezone.utc).isoformat()}

    # -- what-changed, evidence, jobs
    def get_changes(self, driver, ws, document_id, older, newer):
        document = self.workspaces.get(ws, {}).get("documents", {}).get(document_id)
        if not document or older not in document["versions"] or newer not in document["versions"] or older >= newer:
            return None
        cite = lambda n: f"doc:{document_id}:v{newer}:{n:04d}"          # noqa: E731
        return {"items_compared": True, "unchanged_count": 1,
                "added": [{"headline": "A new section", "passages": [{"quote": "Fixture passage that was added.", "chunk_id": cite(3)}]}],
                "removed": [{"headline": "A removed section"}],
                "changed": [{"headline": "Legal Proceedings", "passages": [{"quote": "Fixture passage that changed.", "chunk_id": cite(7)}]}]}

    def evidence(self, driver, ws, chunk_id):
        match = DOC_CHUNK_RE.match(chunk_id)
        document = self.workspaces.get(ws, {}).get("documents", {}).get(match.group(1)) if match else None
        number, chunk = (int(match.group(2)), int(match.group(3))) if match else (0, 0)
        if not document or number not in document["versions"] or not 1 <= chunk <= CHUNKS_PER_DOCUMENT:
            return None
        latest = max(document["versions"])
        return {"id": chunk_id, "text": f"Fixture excerpt from your document, version {number}, paragraph {chunk}.",
                "document_id": match.group(1), "version": number, "is_current": number == latest,
                "status": "current" if number == latest else "superseded", "valid_to": None,
                "superseded_by_version": None if number == latest else latest, "title": document["title"]}

    def get_job(self, driver, ws, job_id):
        return self.workspaces.get(ws, {}).get("jobs", {}).get(job_id)


class JobIds:
    """Job ids in the server's own grammar (16 lower-case hex), counted so a run is reproducible."""

    def __init__(self) -> None:
        self._next = 0x1000
        self._lock = threading.Lock()

    def take(self) -> str:
        with self._lock:
            self._next += 1
            return f"{self._next:016x}"


def job_events(failing: bool, job_id: str, document_id: str, version: int) -> list[dict]:
    events = []
    for event in recorded_events("job_failed" if failing else "job_ready"):
        events.append({**event, "job_id": job_id, "document_id": document_id,
                       "version": None if event["version"] is None else version})
    return events


def make_run_upload_job(fake: FakeWorkspaces):
    ids = JobIds()

    def run_upload_job(app, *, workspace_id, document_id, title, data, kind, content_hash_hex, charged_day=None):
        """Starts a thread that replays the job and returns the job id at once. The thread owns the upload slot's release, as
        the real worker does, and releases it exactly once."""
        job_id = ids.take()
        registry = jobs.registry(app)
        registry.create(workspace_id, job_id)
        failing = FAIL_MARKER in (title or "").lower() or FAIL_MARKER.encode() in data[:64].lower()
        version = fake.next_version(workspace_id, document_id)

        def play():
            try:
                for event in job_events(failing, job_id, document_id, version):
                    time.sleep(JOB_STEP_DELAY_S)
                    if event["state"] == "ready":
                        fake.record_version(workspace_id, document_id, title, content_hash_hex)
                    registry.append(workspace_id, job_id, event)
                    if workspace_id in fake.workspaces:
                        fake.workspaces[workspace_id]["jobs"][job_id] = event
            finally:
                app.state.upload_slots.release()

        threading.Thread(target=play, daemon=True).start()
        return job_id

    return run_upload_job


# ---------------------------------------------------------------- install

def install(app, mp) -> FakeWorkspaces:
    """Wire every fixture into a built app through the monkeypatch ``mp``. Returns the fake workspace store."""
    import sys

    mp.setattr(routes, "aanswer_stream", replay_stream)
    mp.setattr(routes, "astream_workspace_answer", replay_stream)
    mp.setattr(sys.modules["semigraph.agent.stream_async"], "aagent_answer_stream", replay_stream)
    mp.setattr(routes, "run_cypher", public_evidence_rows(routes.run_cypher))
    seed_cache(app)

    fake = FakeWorkspaces()
    for name in ("authenticate", "touch", "create_workspace", "get_workspace", "delete_workspace", "quota", "latest_version",
                 "get_changes", "evidence", "get_job"):
        mp.setattr(repo, name, getattr(fake, name))
    mp.setattr(store, "reserve_daily_upload", lambda driver, limit, day=None: True)
    mp.setattr(jobs, "run_upload_job", make_run_upload_job(fake))
    app.state.uploads_ready = True
    app.state.upload_slots = threading.BoundedSemaphore(1)
    app.state.upload_jobs = jobs.JobRegistry()
    app.state.workspace_create_limiter = RateLimiter(GENEROUS, 86400)
    app.state.upload_limiter = RateLimiter(GENEROUS, 3600)
    return fake


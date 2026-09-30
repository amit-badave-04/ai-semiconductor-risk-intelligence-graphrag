"""ALL Cypher for the ``User*`` labels (M4, docs/v2/M4_PLAN.md 4.2, 4.4 and 15.4).

Invariants ``tests/test_serve_upload_repo.py`` checks statically on every module-level query string (named ``*_QUERY``,
plus the ``_DELETE_LABEL_TEMPLATE`` formatted once per label in :data:`_USER_LABELS`):

- every statement binds ``$ws``, and every ``User*`` node pattern that carries an inline property map spells
  ``workspace_id: $ws`` in it — EXCEPT :data:`SWEEP_SELECT_QUERY`, :data:`SWEEP_ORPHANS_SELECT_QUERY` and
  :data:`FAIL_INTERRUPTED_JOBS_SELECT_QUERY`, which by design look across every workspace at once (the documented
  exceptions; the actual deletion/update each of them drives still runs the normal per-workspace, ``$ws``-bound
  queries, one workspace/job at a time);
- no relationship ever touches a public label, and each private node carries exactly one label;
- tokens: only ``sha256(token)`` is stored; :func:`authenticate` compares with ``hmac.compare_digest`` against a
  dummy hash of the same shape for an unknown or expired workspace, so an unknown id and a wrong token look alike;
- vector search uses the filtered ``SEARCH ... WHERE c.workspace_id = $ws ...`` form on ``user_chunk_embedding``
  (never an unfiltered ``db.index.vector.queryNodes``; verified live against the throwaway instance, S2);
- :func:`put_version` writes the version, its units, chunks, embeddings, change passages and the currency flip in
  ONE transaction (``session.execute_write``), and that same transaction LOCKS the ``UserWorkspace`` node FIRST
  (docs/v2/M4_PLAN.md 15.4): a workspace deleted (or TTL-swept) mid-job must leave nothing behind, so the write
  refuses (raises :class:`WorkspaceGone`) rather than creating a document/version/chunk for a workspace that is
  already gone. :func:`put_job`, :func:`delete_workspace` and the sweep all take the SAME lock first, so a writer
  and a delete can never race each other into leaving an orphan.

Every ``valid_from`` / ``valid_to`` / ``expires_at`` / ``created_at`` / ``now`` / ``cutoff`` that crosses into Neo4j is a
timezone-aware ``datetime`` (never an ISO string — see :mod:`semigraph.uploads.versions`), and every value coming back
out that a caller will serialize to JSON is converted with ``toString(...)`` in the ``RETURN`` clause. Log lines here
never carry chunk text, headlines or quotes — only ids, counts and status strings (never uploaded bytes or text).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta

from ..graph.client import NO_UNRECOGNIZED_NOTIFICATIONS, run_cypher
from ..versions import CURRENT, SUPERSEDED
from . import new_workspace_id, new_workspace_token
from .versions import CURRENT_VALID_TO

logger = logging.getLogger("semigraph.uploads.repo")

# A sha256 hex digest that is not, and will never be, a real token's hash: compared against on every failed lookup so
# an unknown workspace id costs the same hmac.compare_digest call as a wrong token on a real one.
_DUMMY_TOKEN_HASH = hashlib.sha256(b"semigraph-m4-dummy-token-hash").hexdigest()

_USER_LABELS = ("UserJob", "UserPassage", "UserChunk", "UserUnit", "UserVersion", "UserDocument", "UserWorkspace")
_DELETE_LABEL_TEMPLATE = "MATCH (n:{label} {{workspace_id: $ws}}) DETACH DELETE n"
SWEEP_SELECT_BATCH = 500


class WorkspaceGone(Exception):
    """Raised by :func:`put_version` and :func:`put_job` when the ``UserWorkspace`` node they lock first no longer
    exists — deleted by :func:`delete_workspace` or the TTL sweep while a job was still writing
    (docs/v2/M4_PLAN.md 15.4). Callers (``uploads.jobs``) map this to a fixed ``workspace_deleted`` job failure and
    write nothing further for that workspace, not even the failure event itself. Carries no workspace id in its
    message (never logged, but kept id-free on principle — docs/v2/M4_PLAN.md 5)."""


def _require_aware_datetime(value: datetime, name: str) -> None:
    """The one contract every caller (Worker C's jobs.py and workspace_routes.py included) must honor for a
    caller-supplied timestamp: timezone-aware, never naive and never an ISO string. A naive ``datetime`` (e.g.
    ``datetime.utcnow()``) or a string silently breaks the ``SEARCH`` range filter and the ``sweep_expired``
    comparison — no Cypher error, just an empty or wrong result (verified live against the throwaway instance, and
    the reason :mod:`semigraph.uploads.versions` exists)."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"{name} must be a timezone-aware datetime, got {value!r}")

# ---------------------------------------------------------------- workspace lifecycle

CREATE_WORKSPACE_QUERY = """CREATE (w:UserWorkspace {workspace_id: $ws, token_hash: $token_hash, created_at: $now,
    expires_at: $expires_at, last_seen: $now, embedded_tokens: 0})"""

AUTH_LOOKUP_QUERY = """MATCH (w:UserWorkspace {workspace_id: $ws})
RETURN w.token_hash AS token_hash, w.expires_at AS expires_at"""

TOUCH_QUERY = "MATCH (w:UserWorkspace {workspace_id: $ws}) SET w.last_seen = $now"

SWEEP_SELECT_QUERY = f"""MATCH (w:UserWorkspace) WHERE w.expires_at < $now
RETURN w.workspace_id AS workspace_id LIMIT {SWEEP_SELECT_BATCH}"""

# The FIRST statement of every writer that must never leave an orphan behind a concurrent delete/sweep
# (docs/v2/M4_PLAN.md 15.4): takes the node's write lock (the ``SET`` — a no-op value change, but Neo4j locks on
# any ``SET`` to the matched node) BEFORE that writer's own reads/writes, so it serializes against delete_workspace
# and the sweep instead of racing them. Zero rows back means the workspace no longer exists.
LOCK_WORKSPACE_QUERY = """MATCH (w:UserWorkspace {workspace_id: $ws})
SET w._lock = true
RETURN w.workspace_id AS workspace_id"""

# Cross-workspace by necessity (documented exception, see the module docstring): User* nodes (never UserWorkspace
# itself) whose workspace_id has no matching UserWorkspace node at all — the defence-in-depth pass behind
# put_version/put_job's own lock-and-refuse guard, for anything that guard did not anticipate (docs/v2/M4_PLAN.md
# 15.4, findings 2/13/24). Batched like SWEEP_SELECT_QUERY; call again to keep sweeping a larger backlog.
#
# ONE UNION member per label, each with a LITERAL label in its MATCH — never a bound `$labels` list matched with
# `label IN labels(n)` over a single unlabelled `MATCH (n)`, which EXPLAIN confirms forces an AllNodesScan (a full
# scan of the ENTIRE graph, every public label included, once per label in the list) on the throwaway instance. A
# literal label lets the planner use a NodeByLabelScan instead — the difference between touching a few hundred
# User* nodes and touching the whole graph every 15 minutes, now that the sweeper always runs (finding 26).
#
# CALL () { ... } — the EXPLICIT, empty variable-scope clause (round-4 review, finding R4): plain `CALL { ... }`
# (no scope clause at all) is deprecated on Neo4j 2026.07.1 and logs a DEPRECATION notification on every single
# sweep cycle (the sweeper runs this every 15 minutes, always, since finding 26). This subquery imports no outer
# variable, so `()` is correct, not `(x)` for some `x`.
_SWEEP_ORPHANS_LABELS = tuple(label for label in _USER_LABELS if label != "UserWorkspace")
_SWEEP_ORPHANS_UNION_MEMBER = """MATCH (n:{label}) WHERE n.workspace_id IS NOT NULL
  AND NOT EXISTS {{ MATCH (w:UserWorkspace) WHERE w.workspace_id = n.workspace_id }}
RETURN n.workspace_id AS workspace_id"""
SWEEP_ORPHANS_SELECT_QUERY = "CALL () {{\n{members}\n}}\nRETURN DISTINCT workspace_id LIMIT {batch}".format(
    members="\nUNION\n".join(_SWEEP_ORPHANS_UNION_MEMBER.format(label=label) for label in _SWEEP_ORPHANS_LABELS),
    batch=SWEEP_SELECT_BATCH)

# Cross-workspace by necessity (documented exception): any UserJob left in a non-terminal state past
# FAIL_INTERRUPTED_AFTER_S — a process that crashed, OOM'd or was redeployed mid-job (finding 27). The per-job
# UPDATE below stays $ws-bound and re-checks the state, so a job that finished in the meantime is never clobbered.
FAIL_INTERRUPTED_JOBS_SELECT_QUERY = f"""MATCH (j:UserJob) WHERE NOT j.state IN $terminal_states
  AND j.updated_at < $threshold
RETURN j.workspace_id AS workspace_id, j.job_id AS job_id, j.payload AS payload LIMIT {SWEEP_SELECT_BATCH}"""

FAIL_INTERRUPTED_JOB_UPDATE_QUERY = """MATCH (j:UserJob {workspace_id: $ws, job_id: $job_id})
WHERE NOT j.state IN $terminal_states
SET j.state = $state, j.payload = $payload, j.updated_at = $now
RETURN j.job_id AS job_id"""

# Round-4 review, finding 27 residual 1: before EVER marking a stale job "failed", check whether its OWN
# (document_id, version) already has a committed UserVersion — the terminal "ready" write can keep failing (its own
# retry budget is independent of put_version's) even though put_version's transaction fully committed. $ws-bound,
# like every other per-job follow-up query here.
#
# Round-4 review 3, finding 27 residual 2 (secRel LOW repo.py:635 / corUi LOW repo.py:130): matching only on
# (document_id, version) recovers ANY job to "ready" off ANY job's committed version of that same document/version
# pair — including a DIFFERENT job's (a job that legitimately failed, later "recovered" by a re-upload; or a job
# whose own version never committed while a later job committed the same document/version). `v.job_id = $job_id`
# requires the committed UserVersion to have been written BY THIS JOB (put_version now stores job_id inside its
# single transaction) — a version some OTHER job committed never recovers this one; it falls through to interrupted.
JOB_VERSION_EXISTS_QUERY = """MATCH (v:UserVersion {workspace_id: $ws, document_id: $document_id, version: $version})
WHERE v.job_id = $job_id
OPTIONAL MATCH (v)-[:HAS_CHUNK]->(c:UserChunk {workspace_id: $ws})
WITH v, count(c) AS chunks
OPTIONAL MATCH (v)-[:HAS_UNIT]->(u:UserUnit {workspace_id: $ws})
RETURN chunks, count(u) AS units, v.items_compared AS items_compared,
       v.not_compared_reason AS not_compared_reason, v.suspicious AS suspicious"""

# uploads.jobs.TERMINAL_STATES, duplicated here (a plain tuple, never an import of uploads.jobs — this module stays
# strictly below the job layer, never above it) so fail_interrupted_jobs never needs jobs.py to be importable.
_JOB_TERMINAL_STATES = ("ready", "failed")
# uploads.jobs.JOB_ERROR_MESSAGES["interrupted"], duplicated for the same reason: an exact fixed string, never a
# raw exception message (docs/v2/M4_PLAN.md 5) — kept in sync with jobs.py by tests/test_serve_upload_repo.py.
_INTERRUPTED_ERROR_MESSAGE = "processing was interrupted and could not finish"
# At least a ~90 s parse plus the 1,200 s embed budget, with generous slack: a job that has not moved in this long
# was abandoned by a dead process, not merely slow.
FAIL_INTERRUPTED_AFTER_S = 35 * 60   # default only: above parse (90 s) + embed (1,200 s) + a 10 min margin


def create_workspace(driver, ttl_hours: int) -> tuple[str, str, str]:
    """Create a workspace; returns ``(workspace_id, token, expires_at_iso)``. The token is shown once and never stored."""
    workspace_id = new_workspace_id()
    token = new_workspace_token()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=ttl_hours)
    run_cypher(driver, CREATE_WORKSPACE_QUERY, ws=workspace_id,
               token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(), now=now, expires_at=expires_at)
    logger.info("workspace created ws_hash=%s ttl_hours=%d", _ws_hash(workspace_id), ttl_hours)
    return workspace_id, token, expires_at.isoformat()


def authenticate(driver, workspace_id: str, token: str) -> bool:
    """True when ``token`` opens ``workspace_id`` and it has not expired (constant-time; see module docstring)."""
    now = datetime.now(UTC)
    rows = run_cypher(driver, AUTH_LOOKUP_QUERY, ws=workspace_id)
    row = rows[0] if rows else None
    live = row is not None and row["expires_at"] is not None and row["expires_at"] > now
    stored_hash = row["token_hash"] if live else _DUMMY_TOKEN_HASH
    given_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    # The digest compare ALWAYS runs, unconditionally — `live and ...` would short-circuit it for an unknown or
    # expired workspace and skip exactly the comparison whose cost must look the same on every path.
    match = hmac.compare_digest(given_hash, stored_hash)
    return live and match


def touch(driver, ws: str) -> None:
    run_cypher(driver, TOUCH_QUERY, ws=ws, now=datetime.now(UTC))


def _ws_hash(ws: str) -> str:
    """Never log a raw workspace id (docs/v2/M4_PLAN.md 5) — only its sha256 prefix."""
    return hashlib.sha256(ws.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- workspace / document reads

GET_WORKSPACE_QUERY = """MATCH (w:UserWorkspace {workspace_id: $ws})
RETURN w.workspace_id AS workspace_id, toString(w.expires_at) AS expires_at, w.embedded_tokens AS embedded_tokens"""

GET_DOCUMENTS_QUERY = """MATCH (d:UserDocument {workspace_id: $ws})
RETURN d.document_id AS document_id, d.title AS title, d.latest_version AS latest_version
ORDER BY d.created_at"""

GET_VERSIONS_QUERY = """MATCH (v:UserVersion {workspace_id: $ws, document_id: $document_id})
RETURN v.version AS version, toString(v.created_at) AS created_at, v.pages AS pages, v.chars AS chars,
       v.is_current AS is_current, v.status AS status, v.items_compared AS items_compared,
       v.not_compared_reason AS not_compared_reason, v.suspicious AS suspicious
ORDER BY v.version"""

QUOTA_QUERY = """MATCH (w:UserWorkspace {workspace_id: $ws})
OPTIONAL MATCH (d:UserDocument {workspace_id: $ws})
OPTIONAL MATCH (d)-[:HAS_VERSION]->(v:UserVersion {workspace_id: $ws})
WITH w, d, count(v) AS version_count
OPTIONAL MATCH (d)-[:HAS_VERSION]->(cur:UserVersion {workspace_id: $ws, is_current: true})
RETURN w.embedded_tokens AS embedded_tokens, d.document_id AS document_id, version_count,
       coalesce(cur.pages, 0) AS current_pages"""


def get_workspace(driver, ws: str) -> dict | None:
    rows = run_cypher(driver, GET_WORKSPACE_QUERY, ws=ws)
    if not rows:
        return None
    workspace = rows[0]
    documents = [_document_with_versions(driver, ws, d) for d in run_cypher(driver, GET_DOCUMENTS_QUERY, ws=ws)]
    return {
        "workspace_id": workspace["workspace_id"],
        "expires_at": workspace["expires_at"],
        "documents": documents,
        "usage": {"documents": len(documents),
                  "pages": sum(sum(v["pages"] or 0 for v in d["versions"] if v["is_current"]) for d in documents),
                  "embedded_tokens": workspace["embedded_tokens"] or 0},
    }


def _document_with_versions(driver, ws: str, document_row: dict) -> dict:
    versions = run_cypher(driver, GET_VERSIONS_QUERY, ws=ws, document_id=document_row["document_id"])
    return {**document_row, "versions": versions}


def quota(driver, ws: str) -> dict:
    """``pages_by_document`` (docs/v2/M4_PLAN.md 15.8, C3 item 5) lets a caller checking the workspace-wide page cap
    subtract a document's OWN current pages before adding its new version's pages — so a same-size re-version of a
    document already at the cap is accepted, while a brand-new document that would push the total over stays
    refused. ``pages`` (the plain total) is kept for backward compatibility with any caller that only needs that."""
    rows = run_cypher(driver, QUOTA_QUERY, ws=ws)
    if not rows:
        return {"documents": 0, "versions_by_document": {}, "pages": 0, "pages_by_document": {},
                "embedded_tokens": 0}
    docs = [r for r in rows if r["document_id"] is not None]
    return {
        "documents": len(docs),
        "versions_by_document": {r["document_id"]: r["version_count"] for r in docs},
        "pages": sum(r["current_pages"] for r in docs),
        "pages_by_document": {r["document_id"]: r["current_pages"] for r in docs},
        "embedded_tokens": rows[0]["embedded_tokens"] or 0,
    }


# ---------------------------------------------------------------- version reads

LATEST_VERSION_QUERY = """MATCH (v:UserVersion {workspace_id: $ws, document_id: $document_id, is_current: true})
RETURN v.version AS version, v.content_hash AS content_hash, v.method AS method, v.chars_per_page AS chars_per_page"""

VERSION_TEXT_QUERY = """MATCH (v:UserVersion {workspace_id: $ws, document_id: $document_id, version: $version})
RETURN v.text AS text, v.method AS method, v.chars_per_page AS chars_per_page"""

VERSION_UNITS_QUERY = """MATCH (u:UserUnit {workspace_id: $ws, document_id: $document_id, version: $version})
RETURN u.unit_id AS unit_id, u.kind AS kind, u.headline AS headline, u.char_start AS char_start, u.char_end AS char_end
ORDER BY u.char_start"""

VERSION_CHUNK_SPANS_QUERY = """MATCH (c:UserChunk {workspace_id: $ws, document_id: $document_id, version: $version})
RETURN c.chunk_id AS chunk_id, c.char_start AS char_start, c.char_end AS char_end
ORDER BY c.seq"""

EMBEDDED_CHUNKS_QUERY = """MATCH (c:UserChunk {workspace_id: $ws, document_id: $document_id})
WHERE c.text_hash IS NOT NULL
RETURN c.text_hash AS text_hash, c.embedding AS embedding"""


def latest_version(driver, ws: str, document_id: str) -> dict | None:
    rows = run_cypher(driver, LATEST_VERSION_QUERY, ws=ws, document_id=document_id)
    return rows[0] if rows else None


def version_view(driver, ws: str, document_id: str, version: int) -> dict | None:
    rows = run_cypher(driver, VERSION_TEXT_QUERY, ws=ws, document_id=document_id, version=version)
    if not rows:
        return None
    units = run_cypher(driver, VERSION_UNITS_QUERY, ws=ws, document_id=document_id, version=version)
    spans = run_cypher(driver, VERSION_CHUNK_SPANS_QUERY, ws=ws, document_id=document_id, version=version)
    return {**rows[0], "units": units,
            "chunk_spans": [[s["chunk_id"], s["char_start"], s["char_end"]] for s in spans]}


def embedded_chunks(driver, ws: str, document_id: str) -> dict:
    rows = run_cypher(driver, EMBEDDED_CHUNKS_QUERY, ws=ws, document_id=document_id)
    return {r["text_hash"]: r["embedding"] for r in rows}


# ---------------------------------------------------------------- put_version (one transaction)

# UserDocument.title tracks the LATEST version's file name (the document list and its "New version of <title>"
# option both rely on this — unchanged by Post-G10 fix 2 below). CREATE_VERSION_QUERY additionally stamps that
# SAME title onto the UserVersion node itself, so a chunk-level read (the "reads after write" section further
# down) can show the file name a specific version was actually uploaded under, not whatever the document has since
# been renamed to by a later version.
MERGE_DOCUMENT_QUERY = """MATCH (w:UserWorkspace {workspace_id: $ws})
MERGE (d:UserDocument {workspace_id: $ws, document_id: $document_id})
ON CREATE SET d.created_at = $now, d.title = $title
SET d.latest_version = $version
MERGE (w)-[:OWNS]->(d)"""

SET_DOCUMENT_TITLE_QUERY = """MATCH (d:UserDocument {workspace_id: $ws, document_id: $document_id})
SET d.title = $title"""

FIND_CURRENT_VERSION_QUERY = """MATCH (:UserDocument {workspace_id: $ws, document_id: $document_id})
      -[:HAS_VERSION]->(cur:UserVersion {workspace_id: $ws, document_id: $document_id, is_current: true})
RETURN cur.version AS version"""

CREATE_VERSION_QUERY = """MATCH (d:UserDocument {workspace_id: $ws, document_id: $document_id})
CREATE (v:UserVersion {workspace_id: $ws, document_id: $document_id, version: $version,
    version_key: $version_key, content_hash: $content_hash, method: $method, pages: $pages, chars: $chars,
    chars_per_page: $chars_per_page, text: $text, created_at: $now, is_current: true,
    valid_from: $now, valid_to: $current_valid_to, status: $status, items_compared: $items_compared,
    not_compared_reason: $not_compared_reason, suspicious: $suspicious, job_id: $job_id,
    title: coalesce($title, d.title)})
CREATE (d)-[:HAS_VERSION]->(v)"""

SUPERSEDE_VERSION_QUERY = """MATCH (newer:UserVersion {workspace_id: $ws, document_id: $document_id, version: $version})
MATCH (prev:UserVersion {workspace_id: $ws, document_id: $document_id, version: $prev_version})
SET prev.is_current = false, prev.status = $superseded_status, prev.valid_to = $now
CREATE (newer)-[:SUPERSEDES {kind: 'rolled', items_compared: $items_compared,
    not_compared_reason: $not_compared_reason, change_report: $change_report_json}]->(prev)"""

SUPERSEDE_CHUNKS_QUERY = """MATCH (:UserVersion {workspace_id: $ws, document_id: $document_id, version: $prev_version})
      -[:HAS_CHUNK]->(pc:UserChunk {workspace_id: $ws, document_id: $document_id, version: $prev_version})
SET pc.is_current = false, pc.status = $superseded_status, pc.valid_to = $now"""

CREATE_UNIT_QUERY = """MATCH (v:UserVersion {workspace_id: $ws, document_id: $document_id, version: $version})
CREATE (u:UserUnit {workspace_id: $ws, document_id: $document_id, version: $version, unit_id: $unit_id,
    kind: $kind, headline: $headline, text_hash: $text_hash, char_start: $char_start, char_end: $char_end})
CREATE (v)-[:HAS_UNIT]->(u)"""

CREATE_CHUNK_QUERY = """MATCH (v:UserVersion {workspace_id: $ws, document_id: $document_id, version: $version})
CREATE (c:UserChunk {workspace_id: $ws, document_id: $document_id, version: $version, chunk_id: $chunk_id,
    seq: $seq, text: $text, text_hash: $text_hash, char_start: $char_start, char_end: $char_end,
    tokens: $tokens, embedding: $embedding, is_current: true, valid_from: $now, valid_to: $current_valid_to,
    status: $status})
CREATE (v)-[:HAS_CHUNK]->(c)"""

LINK_SUCCEEDED_BY_QUERY = """MATCH (older:UserUnit {workspace_id: $ws, document_id: $document_id,
      version: $older_version, unit_id: $older_unit_id})
MATCH (newer:UserUnit {workspace_id: $ws, document_id: $document_id,
      version: $newer_version, unit_id: $newer_unit_id})
MERGE (older)-[:SUCCEEDED_BY]->(newer)"""

CREATE_PASSAGE_QUERY = """MATCH (newer:UserUnit {workspace_id: $ws, document_id: $document_id,
      version: $newer_version, unit_id: $newer_unit_id})
CREATE (newer)-[:HAS_PASSAGE]->(:UserPassage {workspace_id: $ws, document_id: $document_id,
    older_version: $older_version, newer_version: $newer_version, older_unit_id: $older_unit_id,
    newer_unit_id: $newer_unit_id, kind: $kind, quote: $quote, chunk_id: $chunk_id})"""

BUMP_EMBEDDED_TOKENS_QUERY = """MATCH (w:UserWorkspace {workspace_id: $ws})
SET w.embedded_tokens = w.embedded_tokens + $delta"""


def _merge_document(tx, ws: str, document_id: str, title: str | None, now: datetime, version: int) -> None:
    tx.run(MERGE_DOCUMENT_QUERY, ws=ws, document_id=document_id, now=now, title=title, version=version)
    if title is not None:
        tx.run(SET_DOCUMENT_TITLE_QUERY, ws=ws, document_id=document_id, title=title)


def _create_version(tx, ws: str, document_id: str, version: int, *, content_hash: str, method: str,
                     pages: int, chars: int, chars_per_page: float | None, text: str, now: datetime,
                     items_compared: bool, not_compared_reason: str | None, suspicious: bool,
                     job_id: str | None, title: str | None) -> None:
    tx.run(CREATE_VERSION_QUERY, ws=ws, document_id=document_id, version=version,
           version_key=f"{document_id}:v{version}", content_hash=content_hash, method=method, pages=pages,
           chars=chars, chars_per_page=chars_per_page, text=text, now=now, current_valid_to=CURRENT_VALID_TO,
           status=CURRENT, items_compared=items_compared, not_compared_reason=not_compared_reason,
           suspicious=suspicious, job_id=job_id, title=title)


def _supersede_previous(tx, ws: str, document_id: str, version: int, prev_version: int, now: datetime,
                        items_compared: bool, not_compared_reason: str | None, change_report: dict) -> None:
    tx.run(SUPERSEDE_VERSION_QUERY, ws=ws, document_id=document_id, version=version, prev_version=prev_version,
           now=now, superseded_status=SUPERSEDED, items_compared=items_compared,
           not_compared_reason=not_compared_reason, change_report_json=json.dumps(change_report, default=str))
    tx.run(SUPERSEDE_CHUNKS_QUERY, ws=ws, document_id=document_id, prev_version=prev_version, now=now,
           superseded_status=SUPERSEDED)


def _create_units(tx, ws: str, document_id: str, version: int, units: list[dict]) -> None:
    for u in units:
        tx.run(CREATE_UNIT_QUERY, ws=ws, document_id=document_id, version=version, unit_id=u["unit_id"],
               kind=u["kind"], headline=u.get("headline"), text_hash=u.get("text_hash"),
               char_start=u["char_start"], char_end=u["char_end"])


def _create_chunks(tx, ws: str, document_id: str, version: int, chunks: list[dict], now: datetime) -> int:
    """Creates every chunk of the new version; returns the tokens newly embedded (``embedded`` chunks only)."""
    delta = 0
    for c in chunks:
        embedding = [float(x) for x in c["embedding"]] if c.get("embedding") is not None else None
        tx.run(CREATE_CHUNK_QUERY, ws=ws, document_id=document_id, version=version, chunk_id=c["chunk_id"],
               seq=c["seq"], text=c["text"], text_hash=c["text_hash"], char_start=c["char_start"],
               char_end=c["char_end"], tokens=c.get("tokens"), embedding=embedding, now=now,
               current_valid_to=CURRENT_VALID_TO, status=CURRENT)
        if c.get("embedded"):
            delta += c.get("tokens") or 0
    return delta


def _write_change_passages(tx, ws: str, document_id: str, older_version: int, newer_version: int,
                           changed: list[dict]) -> None:
    for entry in changed:
        older_unit_id, newer_unit_id = entry.get("older_unit_id"), entry.get("newer_unit_id")
        if not (older_unit_id and newer_unit_id):
            continue
        tx.run(LINK_SUCCEEDED_BY_QUERY, ws=ws, document_id=document_id, older_version=older_version,
               older_unit_id=older_unit_id, newer_version=newer_version, newer_unit_id=newer_unit_id)
        for passage in entry.get("passages") or []:
            tx.run(CREATE_PASSAGE_QUERY, ws=ws, document_id=document_id, older_version=older_version,
                   newer_version=newer_version, older_unit_id=older_unit_id, newer_unit_id=newer_unit_id,
                   kind=passage.get("kind"), quote=passage["quote"], chunk_id=passage["chunk_id"])


def put_version(driver, ws: str, *, document_id: str, title: str | None, version: int, content_hash: str,
                 method: str, pages: int, chars: int, chars_per_page: float | None, text: str, units: list[dict],
                 chunks: list[dict], change_report: dict, suspicious: bool, now: datetime,
                 job_id: str | None = None) -> None:
    """One transaction: new document (if any), the new version + its units + chunks, the currency flip of the
    previous version (and its chunks), the SUPERSEDES edge carrying ``change_report``, and the workspace's
    ``embedded_tokens`` counter (the ``SUCCEEDED_BY`` / ``HAS_PASSAGE`` change passages are the change_report's own
    concern).

    ``title`` is stored TWICE (Post-G10 fix 2): merged onto ``UserDocument.title`` as before (always the LATEST
    version's file name — the document list and "New version of <title>" rely on that), and separately stamped on
    this new ``UserVersion`` node itself, so a chunk-level read can later show the file name THIS version was
    actually uploaded under, not whatever the document has since been renamed to by a later version.

    ``get_changes`` answers only an ADJACENT pair (the ``SUPERSEDES`` edge this call writes) — a deliberate scope
    limit, not the full n-choose-2 history (docs/v2/M4_PLAN.md leaves the exact scope to the implementer).

    The FIRST statement of the transaction locks the ``UserWorkspace`` node (:data:`LOCK_WORKSPACE_QUERY`); if it no
    longer exists (deleted or TTL-swept — docs/v2/M4_PLAN.md 15.4), this raises :class:`WorkspaceGone` and writes
    NOTHING — no document, no version, no chunk — rather than creating an orphan.

    ``job_id`` (round-4 review 3, finding 27 residual 2) is stamped on the new ``UserVersion`` node inside this SAME
    transaction — the caller's own job id, when this write is driven by an upload job (``uploads.jobs``). This is
    what :func:`fail_interrupted_jobs` later checks (:data:`JOB_VERSION_EXISTS_QUERY`) before ever recovering a
    stale job to ``ready``: recovery must find a version committed BY THAT JOB, never merely a version that happens
    to share its ``(document_id, version)`` — which a DIFFERENT job could have committed (a job that legitimately
    failed, later misreported as recovered by an unrelated re-upload of the same document). Optional and keyword-only
    with a safe ``None`` default so every existing caller keeps working unchanged; a version written with no job_id
    (or by an old build) simply never matches a later job_id-based recovery check.

    Raises ``TypeError`` if ``now`` is not a timezone-aware ``datetime`` (see :func:`_require_aware_datetime`).
    """
    _require_aware_datetime(now, "now")
    items_compared = bool(change_report.get("items_compared", True))
    not_compared_reason = change_report.get("not_compared_reason")

    def _tx(tx):
        if not tx.run(LOCK_WORKSPACE_QUERY, ws=ws).data():
            raise WorkspaceGone("the workspace no longer exists")
        prev_rows = tx.run(FIND_CURRENT_VERSION_QUERY, ws=ws, document_id=document_id).data()
        prev_version = prev_rows[0]["version"] if prev_rows else None
        _merge_document(tx, ws, document_id, title, now, version)
        _create_version(tx, ws, document_id, version, content_hash=content_hash, method=method, pages=pages,
                        chars=chars, chars_per_page=chars_per_page, text=text, now=now,
                        items_compared=items_compared, not_compared_reason=not_compared_reason,
                        suspicious=suspicious, job_id=job_id, title=title)
        if prev_version is not None:
            _supersede_previous(tx, ws, document_id, version, prev_version, now, items_compared,
                                not_compared_reason, change_report)
        _create_units(tx, ws, document_id, version, units)
        delta = _create_chunks(tx, ws, document_id, version, chunks, now)
        if prev_version is not None:
            _write_change_passages(tx, ws, document_id, prev_version, version, change_report.get("changed") or [])
        if delta:
            tx.run(BUMP_EMBEDDED_TOKENS_QUERY, ws=ws, delta=delta)

    with driver.session() as session:
        session.execute_write(_tx)
    logger.info("put_version ws_hash=%s document_id=%s version=%d chunks=%d units=%d pages=%d",
                _ws_hash(ws), document_id, version, len(chunks), len(units), pages)


# ---------------------------------------------------------------- reads after write

# Every chunk-level read below returns coalesce(v.title, d.title): the CHUNK'S OWN version's title when that
# version stored one, else the document's (current) title — the fallback that keeps a version written before
# Post-G10 fix 2 (no ``v.title`` at all) showing something, rather than ``null``. (Since round 7 a version written
# with no title stores the document's title as it was at upload time, CREATE_VERSION_QUERY.) The version lookup is an
# OPTIONAL MATCH (a chunk's version always exists, but the property may not, on an old version) scoped by
# workspace_id: $ws like every other User* pattern here.
CHUNK_TEXTS_QUERY = """MATCH (c:UserChunk {workspace_id: $ws})
WHERE c.chunk_id IN $chunk_ids
MATCH (d:UserDocument {workspace_id: $ws, document_id: c.document_id})
OPTIONAL MATCH (v:UserVersion {workspace_id: $ws, document_id: c.document_id, version: c.version})
RETURN c.chunk_id AS chunk_id, c.text AS text, c.is_current AS is_current, c.version AS version,
       c.status AS status, toString(c.valid_to) AS valid_to, c.document_id AS document_id,
       coalesce(v.title, d.title) AS title"""

SEARCH_CURRENT_QUERY = """MATCH (c:UserChunk)
SEARCH c IN (VECTOR INDEX user_chunk_embedding FOR $vec WHERE c.workspace_id = $ws AND c.is_current = true
             LIMIT $k) SCORE AS score
MATCH (d:UserDocument {workspace_id: $ws, document_id: c.document_id})
OPTIONAL MATCH (v:UserVersion {workspace_id: $ws, document_id: c.document_id, version: c.version})
RETURN c.chunk_id AS chunk_id, c.text AS text, score, c.document_id AS document_id, c.version AS version,
       c.is_current AS is_current, coalesce(v.title, d.title) AS title
ORDER BY score DESC"""

SEARCH_ASOF_QUERY = """MATCH (c:UserChunk)
SEARCH c IN (VECTOR INDEX user_chunk_embedding FOR $vec
             WHERE c.workspace_id = $ws AND c.valid_from < $cutoff AND c.valid_to >= $cutoff
             LIMIT $k) SCORE AS score
MATCH (d:UserDocument {workspace_id: $ws, document_id: c.document_id})
OPTIONAL MATCH (v:UserVersion {workspace_id: $ws, document_id: c.document_id, version: c.version})
RETURN c.chunk_id AS chunk_id, c.text AS text, score, c.document_id AS document_id, c.version AS version,
       c.is_current AS is_current, coalesce(v.title, d.title) AS title
ORDER BY score DESC"""

GET_CHANGES_QUERY = """MATCH (newer:UserVersion {workspace_id: $ws, document_id: $document_id, version: $newer})
      -[s:SUPERSEDES]->(older:UserVersion {workspace_id: $ws, document_id: $document_id, version: $older})
RETURN s.change_report AS change_report"""

EVIDENCE_QUERY = """MATCH (c:UserChunk {workspace_id: $ws, chunk_id: $chunk_id})
MATCH (d:UserDocument {workspace_id: $ws, document_id: c.document_id})
OPTIONAL MATCH (v:UserVersion {workspace_id: $ws, document_id: c.document_id, version: c.version})
OPTIONAL MATCH (newer:UserVersion {workspace_id: $ws, document_id: c.document_id})
      -[:SUPERSEDES]->(:UserVersion {workspace_id: $ws, document_id: c.document_id, version: c.version})
RETURN c.chunk_id AS id, c.text AS text, c.document_id AS document_id, c.version AS version,
       c.is_current AS is_current, c.status AS status, toString(c.valid_to) AS valid_to,
       newer.version AS superseded_by_version, coalesce(v.title, d.title) AS title"""


def chunk_texts(driver, ws: str, chunk_ids: list[str]) -> dict:
    if not chunk_ids:
        return {}
    rows = run_cypher(driver, CHUNK_TEXTS_QUERY, ws=ws, chunk_ids=chunk_ids)
    return {r["chunk_id"]: {k: v for k, v in r.items() if k != "chunk_id"} for r in rows}


def search_chunks(driver, ws: str, vec: list[float], k: int, cutoff: datetime | None) -> list[dict]:
    """Raises ``TypeError`` if ``cutoff`` is given but is not a timezone-aware ``datetime``."""
    if cutoff is None:
        return run_cypher(driver, SEARCH_CURRENT_QUERY, ws=ws, vec=vec, k=k)
    _require_aware_datetime(cutoff, "cutoff")
    return run_cypher(driver, SEARCH_ASOF_QUERY, ws=ws, vec=vec, k=k, cutoff=cutoff)


def get_changes(driver, ws: str, document_id: str, older: int, newer: int) -> dict | None:
    rows = run_cypher(driver, GET_CHANGES_QUERY, ws=ws, document_id=document_id, older=older, newer=newer)
    if not rows or rows[0]["change_report"] is None:
        return None
    return json.loads(rows[0]["change_report"])


def evidence(driver, ws: str, chunk_id: str) -> dict | None:
    rows = run_cypher(driver, EVIDENCE_QUERY, ws=ws, chunk_id=chunk_id)
    return rows[0] if rows else None


# ---------------------------------------------------------------- deletion

WORKSPACE_EXISTS_QUERY = "MATCH (w:UserWorkspace {workspace_id: $ws}) RETURN count(w) AS n"


def _delete_user_nodes(tx, ws: str) -> None:
    for label in _USER_LABELS:
        tx.run(_DELETE_LABEL_TEMPLATE.format(label=label), ws=ws)


def delete_workspace(driver, ws: str) -> bool:
    """Every ``User*`` node of the workspace, gone in one transaction. True when the workspace existed.

    Locks the ``UserWorkspace`` node FIRST (:data:`LOCK_WORKSPACE_QUERY`, the same lock :func:`put_version` and
    :func:`put_job` take), so a job mid-write and a delete can never race each other into leaving an orphan
    (docs/v2/M4_PLAN.md 15.4): whichever gets the lock first commits fully before the other one even starts.
    """
    def _tx(tx):
        existed = bool(tx.run(LOCK_WORKSPACE_QUERY, ws=ws).data())
        _delete_user_nodes(tx, ws)
        return existed

    with driver.session() as session:
        existed = session.execute_write(_tx)
    logger.info("workspace deleted ws_hash=%s existed=%s", _ws_hash(ws), existed)
    return existed


def sweep_expired(driver, now: datetime) -> int:
    """Delete every workspace whose ``expires_at < now`` (idempotent; up to :data:`SWEEP_SELECT_BATCH` per call —
    call again to keep sweeping a larger backlog). Returns the number of workspaces actually deleted.

    Each per-workspace transaction locks the ``UserWorkspace`` node first, exactly like :func:`delete_workspace`
    and :func:`put_version` (docs/v2/M4_PLAN.md 15.4); a workspace already deleted by the time its turn comes
    (a concurrent explicit DELETE) is simply skipped, not double-counted or errored.

    Raises ``TypeError`` if ``now`` is not a timezone-aware ``datetime``: a naive value or an ISO string would
    silently match NOTHING (an empty ``expired`` list, no error) and break the 24 h retention promise forever.
    """
    _require_aware_datetime(now, "now")

    def _delete_if_locked(tx, ws: str) -> bool:
        if not tx.run(LOCK_WORKSPACE_QUERY, ws=ws).data():
            return False
        _delete_user_nodes(tx, ws)
        return True

    with driver.session() as session:
        expired = [r["workspace_id"] for r in session.run(SWEEP_SELECT_QUERY, now=now).data()]
        deleted = sum(1 for ws in expired if session.execute_write(lambda tx, ws=ws: _delete_if_locked(tx, ws)))
    if deleted:
        logger.info("swept %d expired workspace(s)", deleted)
    return deleted


def sweep_orphans(driver, now: datetime) -> int:
    """Deletes ``User*`` nodes (never ``UserWorkspace`` itself) whose ``workspace_id`` matches no ``UserWorkspace``
    node at all — the defence-in-depth pass behind :func:`put_version` / :func:`put_job`'s own lock-and-refuse guard
    (docs/v2/M4_PLAN.md 15.4, findings 2/13/24): anything written by a path that guard did not anticipate still gets
    cleaned up. Idempotent and batched (:data:`SWEEP_SELECT_BATCH` per call; call again for a larger backlog).
    ``now`` is accepted (and validated) for symmetry with :func:`sweep_expired` and :func:`fail_interrupted_jobs`,
    which both need a caller-supplied instant; this sweep itself has no age-based filter, only "orphaned or not".
    Returns the number of orphaned workspace ids cleaned up.
    """
    _require_aware_datetime(now, "now")

    def _delete_if_still_orphaned(tx, ws: str) -> bool:
        # Re-verify INSIDE the delete transaction: defends against the vanishingly unlikely race of a brand-new
        # workspace reusing the same (128-bit random) id between the select above and this delete.
        if tx.run(WORKSPACE_EXISTS_QUERY, ws=ws).single()["n"] > 0:
            return False
        for label in _SWEEP_ORPHANS_LABELS:
            tx.run(_DELETE_LABEL_TEMPLATE.format(label=label), ws=ws)
        return True

    with driver.session() as session:
        orphans = [r["workspace_id"] for r in session.run(SWEEP_ORPHANS_SELECT_QUERY).data()]
        cleaned = sum(1 for ws in orphans
                     if session.execute_write(lambda tx, ws=ws: _delete_if_still_orphaned(tx, ws)))
    if cleaned:
        logger.info("swept %d orphaned workspace id(s) with no UserWorkspace node", cleaned)
    return cleaned


# ---------------------------------------------------------------- jobs

# A plain MATCH does not take a write lock and does not wait on delete_workspace's/the sweep's in-flight
# transaction (Neo4j reads see only committed data — they never block on it): a version without the SAME
# ``SET w._lock = true`` :func:`put_version` and :func:`delete_workspace` take could still read the workspace as
# "live" a moment before the delete commits, then MERGE a UserJob that the (already-past-its-UserJob-delete-
# statement) transaction never touches — an orphan even though every individual step looks "guarded"
# (docs/v2/M4_PLAN.md 15.4). Taking the SAME lock first forces this single-statement, auto-commit write to
# serialize against every other lock-taker on this node.
PUT_JOB_QUERY = """MATCH (w:UserWorkspace {workspace_id: $ws})
SET w._lock = true
MERGE (j:UserJob {workspace_id: $ws, job_id: $job_id})
ON CREATE SET j.created_at = $now
SET j.state = $state, j.document_id = $document_id, j.version = $version, j.payload = $payload, j.updated_at = $now
RETURN j.job_id AS job_id"""

GET_JOB_QUERY = """MATCH (j:UserJob {workspace_id: $ws, job_id: $job_id}) RETURN j.payload AS payload"""


def put_job(driver, ws: str, job: dict) -> None:
    """Raises :class:`WorkspaceGone` when the ``UserWorkspace`` node no longer exists (docs/v2/M4_PLAN.md 15.4):
    the leading ``MATCH`` + lock means the ``MERGE`` never runs at all for a deleted/swept workspace — zero rows
    back, zero writes, never a UserJob orphan."""
    rows = run_cypher(driver, PUT_JOB_QUERY, ws=ws, job_id=job["job_id"], state=job.get("state"),
                      document_id=job.get("document_id"), version=job.get("version"),
                      payload=json.dumps(job, default=str), now=datetime.now(UTC))
    if not rows:
        raise WorkspaceGone("the workspace no longer exists")


def _recovered_or_interrupted_payload(session, ws: str, job_id: str, payload: dict) -> tuple[dict, str]:
    """The #27 residual (round-4 reliability review): a job's TERMINAL ``ready`` write can keep failing AFTER
    ``put_version`` already committed the version — the two have independent retry budgets, so one can exhaust its
    attempts while the other quietly succeeds. Marking such a job ``failed``/``interrupted`` would misreport a
    fully-succeeded upload; leaving its stale non-terminal state to replay forever would be just as wrong. When the
    job's own ``(document_id, version)`` already has a committed ``UserVersion`` written BY THIS SAME ``job_id``
    (round-4 review 3, finding 27 residual 2 — never merely a version some OTHER job committed for the same
    document/version pair), this reconstructs the SAME ``ready`` payload ``uploads.jobs._process`` would have
    persisted — straight from that version's own data, never from the job's stale payload — instead of the usual
    ``interrupted`` one. Returns ``(new_payload, new_state)``."""
    document_id, version = payload.get("document_id"), payload.get("version")
    if document_id is not None and version is not None:
        rows = session.run(JOB_VERSION_EXISTS_QUERY, ws=ws, document_id=document_id, version=version,
                           job_id=job_id).data()
        if rows:
            v = rows[0]
            ready_payload = {**payload, "state": "ready", "chunks": v["chunks"], "units": v["units"],
                             "items_compared": v["items_compared"], "not_compared_reason": v["not_compared_reason"],
                             "suspicious": v["suspicious"]}
            ready_payload.pop("error", None)
            return ready_payload, "ready"
    interrupted_payload = {**payload, "state": "failed",
                           "error": {"code": "interrupted", "message": _INTERRUPTED_ERROR_MESSAGE}}
    return interrupted_payload, "failed"


def fail_interrupted_jobs(driver, now: datetime | None = None, *, older_than_s: int = FAIL_INTERRUPTED_AFTER_S,
                          exclude: frozenset[tuple[str, str]] | set[tuple[str, str]] = frozenset()
                          ) -> tuple[int, int]:
    """Marks ``failed`` (error code ``interrupted``) any ``UserJob`` left in a non-terminal state for longer than
    ``older_than_s`` — a process that crashed, OOM'd or was redeployed mid-job (docs/v2/M4_PLAN.md 15.4, finding 27) —
    except the ``(workspace_id, job_id)`` pairs in ``exclude`` (the jobs the calling process still has open: a live job's
    progress writes are best-effort, so its stored ``updated_at`` can look stale while it runs). The upload sweeper passes
    a threshold derived from the job budgets (``uploads.jobs``); the default only serves a caller without settings.

    BEFORE marking a candidate ``failed``, checks whether its OWN job id already has a committed ``UserVersion`` for
    its ``(document_id, version)`` (:func:`_recovered_or_interrupted_payload`, finding 27 residual 1, tightened to
    job_id matching by round-4 review 3's finding 27 residual 2) — if so it is instead recovered to ``ready``, never
    mislabelled ``interrupted`` over a version that actually succeeded. A version committed by a DIFFERENT job for
    the same ``(document_id, version)`` — a legitimately failed job later "recovered" off an unrelated re-upload, or
    a job whose own version never committed while a later job committed the same document/version — never matches
    and falls through to ``interrupted``, exactly like a candidate with no committed version at all.

    Rewrites the STORED ``payload`` too, not just ``state``: :func:`get_job` replays ``payload`` verbatim, so leaving it
    alone would keep showing e.g. "embedding" forever to a client that reconnects after a restart. The per-job UPDATE
    stays ``$ws``-bound and re-checks the state, so a job that raced to a real ready/failed in the meantime is left
    untouched (0 rows, not double-counted). ``now`` defaults to the current instant; idempotent; returns
    ``(failed_count, recovered_count)`` — kept as two separate numbers (round-4 review 3) so a caller (the upload
    sweeper) can log a genuine failure and a recovered success as the different events they are, never one figure
    mislabelled "failed".
    """
    now = now if now is not None else datetime.now(UTC)
    _require_aware_datetime(now, "now")
    threshold = now - timedelta(seconds=older_than_s)
    terminal = list(_JOB_TERMINAL_STATES)
    # Quiet on never-written keys, for this SELECT only: UserJob's `state` / `payload` exist only after the first
    # upload, and the sweeper runs this at boot and every cycle. The per-job follow-ups below keep the default session.
    with driver.session(**NO_UNRECOGNIZED_NOTIFICATIONS) as quiet:
        candidates = quiet.run(FAIL_INTERRUPTED_JOBS_SELECT_QUERY, terminal_states=terminal,
                               threshold=threshold).data()
    fixed, recovered = 0, 0
    with driver.session() as session:
        for row in candidates:
            if (row["workspace_id"], row["job_id"]) in exclude:
                continue
            payload = json.loads(row["payload"]) if row["payload"] else {}
            new_payload, new_state = _recovered_or_interrupted_payload(session, row["workspace_id"], row["job_id"],
                                                                       payload)
            updated = session.run(FAIL_INTERRUPTED_JOB_UPDATE_QUERY, ws=row["workspace_id"], job_id=row["job_id"],
                                  terminal_states=terminal, state=new_state,
                                  payload=json.dumps(new_payload, default=str), now=now).data()
            if updated:
                recovered += 1 if new_state == "ready" else 0
                fixed += 1 if new_state == "failed" else 0
    if fixed:
        logger.info("marked %d interrupted job(s) failed at start", fixed)
    if recovered:
        logger.info("recovered %d job(s) to ready: a committed version was found after their terminal write "
                    "kept failing", recovered)
    return fixed, recovered


def get_job(driver, ws: str, job_id: str) -> dict | None:
    rows = run_cypher(driver, GET_JOB_QUERY, ws=ws, job_id=job_id)
    if not rows:
        return None
    return json.loads(rows[0]["payload"])

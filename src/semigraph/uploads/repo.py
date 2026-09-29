"""ALL Cypher for the ``User*`` labels (M4, docs/v2/M4_PLAN.md 4.2 and 4.4).

Invariants ``tests/test_serve_upload_repo.py`` checks statically on every module-level query string (named ``*_QUERY``,
plus the ``_DELETE_LABEL_TEMPLATE`` formatted once per label in :data:`_USER_LABELS`):

- every statement binds ``$ws``, and every ``User*`` node pattern that carries an inline property map spells
  ``workspace_id: $ws`` in it — EXCEPT :data:`SWEEP_SELECT_QUERY`, which by design looks across every workspace at
  once and filters on ``expires_at`` instead (the one documented exception; its actual deletion still runs the normal
  per-workspace, ``$ws``-bound queries, one workspace at a time);
- no relationship ever touches a public label, and each private node carries exactly one label;
- tokens: only ``sha256(token)`` is stored; :func:`authenticate` compares with ``hmac.compare_digest`` against a
  dummy hash of the same shape for an unknown or expired workspace, so an unknown id and a wrong token look alike;
- vector search uses the filtered ``SEARCH ... WHERE c.workspace_id = $ws ...`` form on ``user_chunk_embedding``
  (never an unfiltered ``db.index.vector.queryNodes``; verified live against the throwaway instance, S2);
- :func:`put_version` writes the version, its units, chunks, embeddings, change passages and the currency flip in
  ONE transaction (``session.execute_write``).

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

from ..graph.client import run_cypher
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
    rows = run_cypher(driver, QUOTA_QUERY, ws=ws)
    if not rows:
        return {"documents": 0, "versions_by_document": {}, "pages": 0, "embedded_tokens": 0}
    docs = [r for r in rows if r["document_id"] is not None]
    return {
        "documents": len(docs),
        "versions_by_document": {r["document_id"]: r["version_count"] for r in docs},
        "pages": sum(r["current_pages"] for r in docs),
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

MERGE_DOCUMENT_QUERY = """MERGE (d:UserDocument {workspace_id: $ws, document_id: $document_id})
ON CREATE SET d.created_at = $now, d.title = $title
SET d.latest_version = $version
WITH d
MATCH (w:UserWorkspace {workspace_id: $ws})
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
    not_compared_reason: $not_compared_reason, suspicious: $suspicious})
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
                     items_compared: bool, not_compared_reason: str | None, suspicious: bool) -> None:
    tx.run(CREATE_VERSION_QUERY, ws=ws, document_id=document_id, version=version,
           version_key=f"{document_id}:v{version}", content_hash=content_hash, method=method, pages=pages,
           chars=chars, chars_per_page=chars_per_page, text=text, now=now, current_valid_to=CURRENT_VALID_TO,
           status=CURRENT, items_compared=items_compared, not_compared_reason=not_compared_reason,
           suspicious=suspicious)


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
                 chunks: list[dict], change_report: dict, suspicious: bool, now: datetime) -> None:
    """One transaction: new document (if any), the new version + its units + chunks, the currency flip of the
    previous version (and its chunks), the SUPERSEDES edge carrying ``change_report``, the change passages
    (``SUCCEEDED_BY`` / ``HAS_PASSAGE``), and the workspace's ``embedded_tokens`` counter.

    ``get_changes`` answers only an ADJACENT pair (the ``SUPERSEDES`` edge this call writes) — a deliberate scope
    limit, not the full n-choose-2 history (docs/v2/M4_PLAN.md leaves the exact scope to the implementer).

    Raises ``TypeError`` if ``now`` is not a timezone-aware ``datetime`` (see :func:`_require_aware_datetime`).
    """
    _require_aware_datetime(now, "now")
    items_compared = bool(change_report.get("items_compared", True))
    not_compared_reason = change_report.get("not_compared_reason")

    def _tx(tx):
        prev_rows = tx.run(FIND_CURRENT_VERSION_QUERY, ws=ws, document_id=document_id).data()
        prev_version = prev_rows[0]["version"] if prev_rows else None
        _merge_document(tx, ws, document_id, title, now, version)
        _create_version(tx, ws, document_id, version, content_hash=content_hash, method=method, pages=pages,
                        chars=chars, chars_per_page=chars_per_page, text=text, now=now,
                        items_compared=items_compared, not_compared_reason=not_compared_reason,
                        suspicious=suspicious)
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

CHUNK_TEXTS_QUERY = """MATCH (c:UserChunk {workspace_id: $ws})
WHERE c.chunk_id IN $chunk_ids
MATCH (d:UserDocument {workspace_id: $ws, document_id: c.document_id})
RETURN c.chunk_id AS chunk_id, c.text AS text, c.is_current AS is_current, c.version AS version,
       c.status AS status, toString(c.valid_to) AS valid_to, c.document_id AS document_id, d.title AS title"""

SEARCH_CURRENT_QUERY = """MATCH (c:UserChunk)
SEARCH c IN (VECTOR INDEX user_chunk_embedding FOR $vec WHERE c.workspace_id = $ws AND c.is_current = true
             LIMIT $k) SCORE AS score
MATCH (d:UserDocument {workspace_id: $ws, document_id: c.document_id})
RETURN c.chunk_id AS chunk_id, c.text AS text, score, c.document_id AS document_id, c.version AS version,
       c.is_current AS is_current, d.title AS title
ORDER BY score DESC"""

SEARCH_ASOF_QUERY = """MATCH (c:UserChunk)
SEARCH c IN (VECTOR INDEX user_chunk_embedding FOR $vec
             WHERE c.workspace_id = $ws AND c.valid_from < $cutoff AND c.valid_to >= $cutoff
             LIMIT $k) SCORE AS score
MATCH (d:UserDocument {workspace_id: $ws, document_id: c.document_id})
RETURN c.chunk_id AS chunk_id, c.text AS text, score, c.document_id AS document_id, c.version AS version,
       c.is_current AS is_current, d.title AS title
ORDER BY score DESC"""

GET_CHANGES_QUERY = """MATCH (newer:UserVersion {workspace_id: $ws, document_id: $document_id, version: $newer})
      -[s:SUPERSEDES]->(older:UserVersion {workspace_id: $ws, document_id: $document_id, version: $older})
RETURN s.change_report AS change_report"""

EVIDENCE_QUERY = """MATCH (c:UserChunk {workspace_id: $ws, chunk_id: $chunk_id})
MATCH (d:UserDocument {workspace_id: $ws, document_id: c.document_id})
OPTIONAL MATCH (newer:UserVersion {workspace_id: $ws, document_id: c.document_id})
      -[:SUPERSEDES]->(:UserVersion {workspace_id: $ws, document_id: c.document_id, version: c.version})
RETURN c.chunk_id AS id, c.text AS text, c.document_id AS document_id, c.version AS version,
       c.is_current AS is_current, c.status AS status, toString(c.valid_to) AS valid_to,
       newer.version AS superseded_by_version, d.title AS title"""


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


def delete_workspace(driver, ws: str) -> bool:
    """Every ``User*`` node of the workspace, gone in one transaction. True when the workspace existed."""
    def _tx(tx):
        existed = tx.run(WORKSPACE_EXISTS_QUERY, ws=ws).single()["n"] > 0
        for label in _USER_LABELS:
            tx.run(_DELETE_LABEL_TEMPLATE.format(label=label), ws=ws)
        return existed

    with driver.session() as session:
        existed = session.execute_write(_tx)
    logger.info("workspace deleted ws_hash=%s existed=%s", _ws_hash(ws), existed)
    return existed


def sweep_expired(driver, now: datetime) -> int:
    """Delete every workspace whose ``expires_at < now`` (idempotent; up to :data:`SWEEP_SELECT_BATCH` per call —
    call again to keep sweeping a larger backlog). Returns the number of workspaces deleted.

    Raises ``TypeError`` if ``now`` is not a timezone-aware ``datetime``: a naive value or an ISO string would
    silently match NOTHING (an empty ``expired`` list, no error) and break the 24 h retention promise forever.
    """
    _require_aware_datetime(now, "now")
    with driver.session() as session:
        expired = [r["workspace_id"] for r in session.run(SWEEP_SELECT_QUERY, now=now).data()]
        for ws in expired:
            session.execute_write(lambda tx, ws=ws: [tx.run(_DELETE_LABEL_TEMPLATE.format(label=label), ws=ws)
                                                      for label in _USER_LABELS])
    if expired:
        logger.info("swept %d expired workspace(s)", len(expired))
    return len(expired)


# ---------------------------------------------------------------- jobs

PUT_JOB_QUERY = """MERGE (j:UserJob {workspace_id: $ws, job_id: $job_id})
ON CREATE SET j.created_at = $now
SET j.state = $state, j.document_id = $document_id, j.version = $version, j.payload = $payload, j.updated_at = $now"""

GET_JOB_QUERY = """MATCH (j:UserJob {workspace_id: $ws, job_id: $job_id}) RETURN j.payload AS payload"""


def put_job(driver, ws: str, job: dict) -> None:
    run_cypher(driver, PUT_JOB_QUERY, ws=ws, job_id=job["job_id"], state=job.get("state"),
               document_id=job.get("document_id"), version=job.get("version"),
               payload=json.dumps(job, default=str), now=datetime.now(UTC))


def get_job(driver, ws: str, job_id: str) -> dict | None:
    rows = run_cypher(driver, GET_JOB_QUERY, ws=ws, job_id=job_id)
    if not rows:
        return None
    return json.loads(rows[0]["payload"])

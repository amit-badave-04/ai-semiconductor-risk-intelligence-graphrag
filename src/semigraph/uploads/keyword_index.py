"""The per-workspace keyword index of an upload workspace (docs/specs/m2-search.md R17, S1, S10).

Isolation by construction: an index is built on demand from ONE workspace's current ``UserChunk.text`` rows
(``repo.current_chunk_rows``, bound by ``$ws``) and cached in memory under that workspace's id. There is no shared index, no
index on disk and no connection to the filings index: this module imports only ``build_index`` from the keyword module and never
the fusion or rerank modules (``tests/test_upload_keyword_index.py`` pins the import graph).

The cache is keyed by (workspace id, the current ``(document_id, version)`` pairs). A new version or a deleted document changes
the pairs, so the next question rebuilds; a workspace with no current chunk holds no entry. It is an LRU bounded by entry count
and by an estimate of megabytes, and an entry older than ``max_age_s`` is rebuilt (the workspace itself expires in 24 hours and
is swept; ``invalidate`` drops one at once when its workspace is deleted).
# shortcut: byte size is an estimate (text x 3 + 400 per chunk), not a measurement; revisit if memory pressure shows on the 4 GB machine.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

DEFAULT_MAX_ENTRIES = 16
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_AGE_S = 2 * 3600
RRF_K = 60                                   # the usual reciprocal-rank-fusion constant (the same as the filings fusion)
_ROW_OVERHEAD_BYTES = 400
_TEXT_BYTES_FACTOR = 3                       # the text, its tokens and the index postings, roughly

KEYWORD_ONLY, KEYWORD_SEMANTIC, SEMANTIC_ONLY = "keyword_only", "keyword_semantic", "semantic_only"


@dataclass(frozen=True)
class _Entry:
    key: tuple
    built_at: float
    index: object
    rows: dict
    size: int


class IndexCache:
    def __init__(self, *, max_entries: int = DEFAULT_MAX_ENTRIES, max_bytes: int = DEFAULT_MAX_BYTES,
                 max_age_s: float = DEFAULT_MAX_AGE_S, clock=time.monotonic) -> None:
        self._max_entries, self._max_bytes, self._max_age_s, self._clock = max_entries, max_bytes, max_age_s, clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, workspace_id: str) -> bool:
        return workspace_id in self._entries

    @property
    def size_bytes(self) -> int:
        return sum(e.size for e in self._entries.values())

    def invalidate(self, workspace_id: str) -> None:
        with self._lock:
            self._entries.pop(workspace_id, None)

    def _drop_expired(self) -> None:
        """Entries past ``max_age_s`` go on any ``get``, so a quiet site does not keep an expired workspace's text (S10)."""
        now = self._clock()
        for ws in [w for w, e in self._entries.items() if now - e.built_at > self._max_age_s]:
            del self._entries[ws]

    def get(self, driver, workspace_id: str) -> _Entry | None:
        """The workspace's index, built if absent or stale; ``None`` when it has no current chunk."""
        from . import repo

        key = tuple(sorted((v["document_id"], v["version"]) if isinstance(v, dict) else tuple(v)
                           for v in repo.current_versions(driver, workspace_id)))
        with self._lock:
            self._drop_expired()
            entry = self._entries.get(workspace_id)
            if entry is not None and entry.key == key and self._clock() - entry.built_at <= self._max_age_s:
                self._entries.move_to_end(workspace_id)
                return entry
            self._entries.pop(workspace_id, None)
        if not key:
            return None
        entry = self._build(key, repo.current_chunk_rows(driver, workspace_id))
        with self._lock:
            self._remember(workspace_id, entry)
        return entry

    def _build(self, key: tuple, rows: list[dict]) -> _Entry:
        from ..retrieval.keyword import build_index

        index = build_index([{"chunk_id": r["chunk_id"], "text": r["text"], "is_current": True} for r in rows],
                            with_context=False)
        size = sum(len(r["text"] or "") * _TEXT_BYTES_FACTOR + _ROW_OVERHEAD_BYTES for r in rows)
        return _Entry(key, self._clock(), index, {r["chunk_id"]: r for r in rows}, size)

    def _remember(self, workspace_id: str, entry: _Entry) -> None:
        if entry.size > self._max_bytes:
            return                                               # served for this question, never kept
        self._entries[workspace_id] = entry
        while len(self._entries) > self._max_entries or self.size_bytes > self._max_bytes:
            self._entries.popitem(last=False)


CACHE = IndexCache()


def keyword_rows(driver, workspace_id: str, question: str, k: int) -> list[dict]:
    """The best ``k`` current chunks of THIS workspace for ``question`` by keyword, best first: ``chunk_id``, ``text``,
    ``document_id``, ``version``, ``title``, ``is_current`` (always true) and ``score``. ``[]`` for a workspace with no chunk
    or a question with no term."""
    entry = CACHE.get(driver, workspace_id)
    if entry is None:
        return []
    hits = entry.index.search(question, depth=k)
    return [{**entry.rows[h.chunk_id], "is_current": True, "score": h.score} for h in hits if h.chunk_id in entry.rows]


def rrf_fuse(dense: list[dict], keyword: list[dict], k: int) -> list[dict]:
    """Reciprocal-rank fusion of two ranked row lists into at most ``k`` rows, one per chunk id (the dense row is kept when
    both lists hold the chunk). Ties keep first-seen order."""
    scores: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for ranked in (dense, keyword):
        for rank, row in enumerate(ranked, start=1):
            scores[row["chunk_id"]] = scores.get(row["chunk_id"], 0.0) + 1.0 / (RRF_K + rank)
            rows.setdefault(row["chunk_id"], row)
    order = sorted(scores, key=lambda cid: -scores[cid])        # sorted() is stable: ties stay in first-seen order
    return [rows[cid] for cid in order[:k]]


def search_label(*, dense: list[dict], as_of: bool) -> str:
    """What the upload part of an answer used: keyword only, keyword plus semantic, or (an ``as_of`` ask) semantic only."""
    if as_of:
        return SEMANTIC_ONLY
    return KEYWORD_SEMANTIC if dense else KEYWORD_ONLY

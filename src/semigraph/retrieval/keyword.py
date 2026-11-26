"""In-process keyword (BM25) search over the retrievable EvidenceSpans (docs/specs/m2-search.md R8-R10, OD2, OD4, OD8).

A bm25s index is built once per graph snapshot (API startup, or the bench at run start) and held in memory. It stores chunk
ids and metadata, never text: the winners' text is fetched by id (:func:`hydrate`, ``retrievable`` re-checked), so a stale
index can never show a span the graph no longer serves. The indexed text of a span is its text plus an optional context
field (company, ticker, form, filing year, section, sub-heading); the context field is searchable and never rendered or
returned.

bm25s has no query language, so a question cannot inject syntax. Work is still bounded (S3, S7): one tokenizer, one query
cap (terms, term length, characters), an empty term list gives an empty result, scores are computed once per question and
masked per leg in numpy. bm25s is imported lazily (its Hugging Face loader is never imported; no network client here, S9).
"""

import logging
import re
import sys
import threading
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from functools import cache
from typing import Any, NamedTuple

import numpy as np

from .fusion import FusionRuntime, serving_config

logger = logging.getLogger("semigraph.retrieval.keyword")

TOKENIZER_VERSION = "k1"        # part of the template fingerprint when the flag is on
MAX_QUERY_CHARS = 4_000         # characters of a question the tokenizer reads (S3)
MAX_QUERY_TERMS = 64            # distinct terms of a question that are searched (S3)
MAX_TERM_CHARS = 40             # a longer token is not a word: dropped from the index and from queries (S3)
KEYWORD_DEPTH = 50              # ranks each leg returns (the logged depth of fusion.LOG_DEPTH)
MAX_INDEX_ROWS = 200_000        # the loader refuses a graph with more retrievable spans (it holds ~5,900 today)
STOPWORDS = frozenset({"a", "an", "and", "of", "or", "the", "to"})   # tiny and fixed: bm25's idf handles the rest
_TOKEN = re.compile(r"[^\W_]+(?:-[^\W_]+)*")     # letters and digits, hyphen-joined: 3a090, 10-k, h200, cowos-l
_SCORE_DIGITS = 9                                # scores are rounded before ties are broken by chunk id
_REQUIRED_ROW_KEYS = ("chunk_id", "text")

# One parameterised read: the retrievable spans with what the index needs (filer name and ticker only for the context
# field). ``mention_ciks`` are the Company ciks the span MENTIONS (the anchors' key space).
LOAD_ROWS_QUERY = """MATCH (e:EvidenceSpan) WHERE e.retrievable = true
OPTIONAL MATCH (f:Company {cik: e.filer_cik})
OPTIONAL MATCH (e)-[:MENTIONS]->(m:Company)
WITH e, f, collect(DISTINCT m.cik) AS mention_ciks
RETURN e.chunk_id AS chunk_id, e.text AS text, e.filer_cik AS filer_cik, e.form AS form, e.accession_no AS accession_no,
       e.content_hash AS content_hash, toString(e.valid_from) AS valid_from, e.is_current AS is_current,
       e.section_id AS section_id, e.sub_heading AS sub_heading, toString(e.filing_date) AS filing_date,
       f.name AS company, f.ticker AS ticker, mention_ciks
ORDER BY chunk_id LIMIT $limit"""
# The text of the winning chunk ids, by id; a span that is no longer retrievable is simply not returned.
HYDRATE_QUERY = """MATCH (e:EvidenceSpan) WHERE e.chunk_id IN $ids AND e.retrievable = true
RETURN e.chunk_id AS chunk_id, e.text AS text, e.source_url AS source_url"""


class Hit(NamedTuple):
    """One ranked span: metadata only, no text (see :func:`hydrate`)."""
    chunk_id: str
    score: float
    filer_cik: int | None
    form: str | None
    accession_no: str | None
    content_hash: str | None
    is_current: bool | None
    valid_from: str | None


# --- tokenizer and query handling ---

def tokenize(text: str) -> list[str]:
    """The one tokenizer of the index and of questions: NFKC, lower case, alphanumeric words with internal hyphens kept
    whole (``3a090``, ``10-k``, ``h200``, ``cowos-l``), no stemmer, the tiny fixed stopword list removed, tokens over
    :data:`MAX_TERM_CHARS` dropped. Linear in the text."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return [t for t in _TOKEN.findall(folded) if len(t) <= MAX_TERM_CHARS and t not in STOPWORDS]


def query_terms(query: Any) -> list[str]:
    """The distinct terms of a question, capped (:data:`MAX_QUERY_CHARS`, :data:`MAX_QUERY_TERMS`), in order. A non-string
    or a question with no word gives ``[]``."""
    if not isinstance(query, str):
        return []
    return list(dict.fromkeys(tokenize(query[:MAX_QUERY_CHARS])))[:MAX_QUERY_TERMS]


def context_field(*, company: str | None, ticker: str | None, form: str | None, filing_date: str | None,
                  section_id: str | None, sub_heading: str | None) -> str:
    """The deterministic context text of a span (OD4): company, ticker, form, filing year, section id, sub-heading.
    Indexed with the span's text; never rendered."""
    year = (filing_date or "")[:4]
    return " ".join(str(p) for p in (company, ticker, form, year, section_id, sub_heading) if p)


# --- the index ---

class KeywordIndex:
    """An immutable bm25s index over spans (see the module docstring). Build with :func:`build_index`."""

    def __init__(self, bm25, hits: Sequence[Hit], mentions: Sequence[Iterable[int]], *, with_context: bool, snapshot_id: str):
        self._bm25 = bm25
        self._meta = tuple(hits)
        self.with_context, self.snapshot_id = with_context, snapshot_id
        self._filer = np.array([-1 if h.filer_cik is None else int(h.filer_cik) for h in self._meta], dtype=np.int64)
        self._not_superseded = np.array([h.is_current is True for h in self._meta], dtype=bool)
        self._rank = np.argsort(np.argsort(np.array([h.chunk_id for h in self._meta], dtype=object), kind="stable"), kind="stable")
        positions: dict[int, list[int]] = {}      # cik -> row positions (not a full-length mask per cik: memory at 200k rows)
        for i, ciks in enumerate(mentions):
            for cik in ciks:
                positions.setdefault(int(cik), []).append(i)
        self._by_mention = {cik: np.array(pos, dtype=np.int64) for cik, pos in positions.items()}

    @property
    def size(self) -> int:
        return len(self._meta)

    def _scores(self, query: Any) -> np.ndarray | None:
        terms = query_terms(query)
        if not terms or self._bm25 is None:
            return None
        return np.asarray(self._bm25.get_scores(terms), dtype=np.float64)

    def _top(self, scores: np.ndarray, mask: np.ndarray, depth: int) -> list[Hit]:
        masked = np.where(mask, np.round(scores, _SCORE_DIGITS), 0.0)
        pos = np.flatnonzero(masked > 0)
        if pos.size == 0:
            return []
        order = pos[np.lexsort((self._rank[pos], -masked[pos]))][:depth]
        return [self._meta[i]._replace(score=float(masked[i])) for i in order]

    def _base_mask(self, current_only: bool) -> np.ndarray:
        return self._not_superseded if current_only else np.ones(len(self._meta), dtype=bool)

    def _mention_mask(self, ciks: Iterable[int]) -> np.ndarray:
        mask = np.zeros(len(self._meta), dtype=bool)
        for cik in ciks:
            mask[self._by_mention.get(int(cik), _NO_ROWS)] = True
        return mask

    def search(self, query: Any, *, filer_cik: int | None = None, mention_ciks: Iterable[int] | None = None,
               current_only: bool = False, depth: int = KEYWORD_DEPTH) -> list[Hit]:
        """Ranked hits (best first, ties by chunk id), optionally of one filer, of spans mentioning any of ``mention_ciks``
        and of current filings only."""
        scores = self._scores(query)
        if scores is None:
            return []
        mask = self._base_mask(current_only)
        if filer_cik is not None:
            mask = mask & (self._filer == int(filer_cik))
        if mention_ciks is not None:
            mask = mask & self._mention_mask(mention_ciks)
        return self._top(scores, mask, depth)

    def search_legs(self, query: Any, anchor_ciks: Sequence[int], *, current_only: bool = False, filer_legs: bool = True,
                    depth: int = KEYWORD_DEPTH) -> dict[str, list[Hit]]:
        """The keyword legs of one question, scored ONCE: ``keyword:<cik>`` per anchor (the filer's own spans, only when
        ``filer_legs``) and ``keyword:mention`` (spans that mention any anchor, any filer)."""
        scores = self._scores(query)
        base = self._base_mask(current_only)
        legs: dict[str, list[Hit]] = {}
        for cik in anchor_ciks if filer_legs else []:
            legs[f"keyword:{cik}"] = [] if scores is None else self._top(scores, base & (self._filer == int(cik)), depth)
        legs["keyword:mention"] = ([] if scores is None
                                   else self._top(scores, base & self._mention_mask(anchor_ciks), depth))
        return legs


def build_index(rows: Iterable[Mapping], *, with_context: bool = True, snapshot_id: str = "") -> KeywordIndex:
    """A :class:`KeywordIndex` over ``rows`` (dicts with ``chunk_id``, ``text`` and the optional ``filer_cik``, ``form``,
    ``accession_no``, ``content_hash``, ``valid_from``, ``is_current``, ``mention_ciks``, ``context``). Only ids and
    metadata are kept; ``context`` is indexed with the text when ``with_context`` and is otherwise ignored."""
    bm25s = _bm25s()                   # lazy: the module import stays light, and bm25s's hub loader is never touched

    hits: list[Hit] = []
    mentions: list[list[int]] = []
    corpus: list[list[str]] = []
    for r in rows:
        if any(k not in r for k in _REQUIRED_ROW_KEYS):
            raise ValueError(f"an index row needs {_REQUIRED_ROW_KEYS}")
        hits.append(Hit(r["chunk_id"], 0.0, r.get("filer_cik"), r.get("form"), r.get("accession_no"), r.get("content_hash"),
                        r.get("is_current"), r.get("valid_from")))
        mentions.append(list(r.get("mention_ciks") or []))
        corpus.append(tokenize((r["text"] or "") + (f" {r.get('context') or ''}" if with_context else "")))
    bm25 = None
    if any(corpus):
        bm25 = bm25s.BM25(csc_backend="numpy")      # the numpy CSC backend: no scipy (see _scipy_not_imported)
        bm25.index(corpus, show_progress=False)
    return KeywordIndex(bm25, hits, mentions, with_context=with_context, snapshot_id=snapshot_id)


# --- the loader and the hydration (one read query each, parameters only) ---

_IMPORT_LOCK = threading.Lock()


_NO_ROWS = np.empty(0, dtype=np.int64)


@contextmanager
def _scipy_not_imported():
    """``import bm25s`` imports ``scipy.sparse`` whenever scipy is installed (the serve image installs it for the aligner).
    The API process must never load scipy (tests/test_serve_monitor_isolation.py), and the numpy CSC backend used here does not
    need it, so while bm25s is imported the two names resolve to "not installed" (bm25s then sets its own flag off). A scipy
    that is already loaded is left alone."""
    # shortcut: blocks scipy process-wide while bm25s imports (bm25s 0.3.16 __init__.py:22-26), re-check on any bm25s bump
    names = ("scipy", "scipy.sparse")
    if "scipy" in sys.modules:
        yield
        return
    with _IMPORT_LOCK:
        for name in names:
            sys.modules[name] = None             # an import of these raises ImportError, which bm25s expects
        try:
            yield
        finally:
            for name in names:
                if sys.modules.get(name, 0) is None:
                    del sys.modules[name]


@cache
def _bm25s():
    with _scipy_not_imported():
        import bm25s
    return bm25s


def _read(driver, query: str, **params) -> list[dict]:
    with driver.session(default_access_mode="READ") as session:
        return [dict(r) for r in session.run(query, **params)]


def load_rows(driver, *, max_rows: int = MAX_INDEX_ROWS) -> list[dict]:
    """The retrievable spans of the graph as index rows, with their context field. Refuses more than ``max_rows``."""
    rows = _read(driver, LOAD_ROWS_QUERY, limit=max_rows + 1)      # one more than allowed, so an oversized graph is seen, not cut
    if len(rows) > max_rows:
        raise ValueError(f"the graph holds more than {max_rows} retrievable spans")
    return [{**r, "context": context_field(company=r.get("company"), ticker=r.get("ticker"), form=r.get("form"),
                                           filing_date=r.get("filing_date"), section_id=r.get("section_id"),
                                           sub_heading=r.get("sub_heading"))} for r in rows]


def hydrate(driver, chunk_ids: Sequence[str]) -> dict[str, dict]:
    """``{chunk_id: {"text", "source_url"}}`` for the ids that are still retrievable (the others are absent)."""
    if not chunk_ids:
        return {}
    rows = _read(driver, HYDRATE_QUERY, ids=list(chunk_ids))
    return {r["chunk_id"]: {"text": r["text"], "source_url": r.get("source_url")} for r in rows}


def build_fusion_runtime(driver, settings, snapshot_id: str) -> FusionRuntime | None:
    """The serving fusion runtime for ``RETRIEVAL_FUSION``: None when the flag is off, and None (logged, never raised) when
    the index cannot be built: the fusion path is then not used at all and every ask takes today's path (fail closed)."""
    if not getattr(settings, "retrieval_fusion", False):
        return None
    config = serving_config(settings)
    started = time.perf_counter()
    try:
        index = build_index(load_rows(driver), with_context=config.keyword_context, snapshot_id=snapshot_id)
        if index.size == 0:
            raise ValueError("no retrievable spans to index")
    except Exception as exc:  # noqa: BLE001 - the service must start; fusion just stays off
        logger.error("RETRIEVAL_FUSION is on but the keyword index could not be built (%s%s): serving today's retrieval path",
                     type(exc).__name__, f": {exc}" if isinstance(exc, ValueError) else "")
        return None
    logger.info("keyword index built: %d spans, %.1f s, snapshot %s, tokenizer %s", index.size,
                time.perf_counter() - started, snapshot_id or "none", TOKENIZER_VERSION)
    return FusionRuntime(config, index)

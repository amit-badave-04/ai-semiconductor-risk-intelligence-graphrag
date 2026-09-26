"""Sentence embeddings of risk sections: the SEMANTIC half of the passage adjudicator's candidates (M1b).

The lexical candidates of a sentence (best word overlap, best ``partial_ratio``) miss a paraphrase that shares few words with it,
and the model answers ``different`` when the true counterpart is not among the candidates it is shown. This module finds the
sentences of the OTHER filing's section that are closest to a sentence by cosine similarity of embeddings (Qwen3-Embedding-0.6B,
free and local), so that ``graph/passage_adjudicate`` can list them next to the lexical ones.

* Every sentence span of a section (``align_text.split_sentences``; sentences longer than ``MAX_EMBED_CHARS`` are cut before they
  are embedded) is embedded ONCE as a PASSAGE (no query instruction: both sides are sentences of the same kind) and cached per
  section on disk: ``<dir>/<accession>_<section sha8>.npy`` (float32, one L2-normalised row per span) plus
  ``<dir>/<accession>_<section sha8>.json``, the index (embedder name, full section hash, dim, row count and the sentence spans).
  The ``.npy`` is written first and the index last (both through a temp file and ``os.replace``): a cache without an index is
  incomplete and is rebuilt. Any mismatch (another embedder, another section text, other spans because the sentence splitter
  changed, wrong shape, unreadable file) rebuilds the section; a stale file of an older text is never read and never deleted.
* Sections are embedded lazily, only when a neighbour is asked for, and a re-run reads the cache: nothing is embedded twice.
* :class:`PairNeighbours` answers "the sentences of the other section closest to this sentence": the query vector is the row of the
  sentence's OWN section when its exact span is indexed (it nearly always is: 24 of 19,953 item-level sentences of the lake are not
  section-level spans), otherwise the sentence is embedded on the fly (once).
* The embedder is INJECTED (``list[str] -> array (n, dim)``); :class:`SentenceEmbedder` is the lazy adapter over the project's
  ``Embedder`` (passage-side encode, whatever ``EMBEDDING_BACKEND`` says) that the CLI passes; it loads no model until it is called.

Ranking is deterministic: cosine (a dot product of unit rows), ties to the earlier sentence.
"""

import json
import logging
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from ..hashing import content_hash
from .align_text import split_sentences

logger = logging.getLogger("semigraph.graph.sentence_embed")

EmbedFn = Callable[[list[str]], np.ndarray]
CACHE_VERSION = 1
MAX_EMBED_CHARS = 2000         # a longer "sentence" (a flattened table) is cut for the embedder; the section span stays whole
EMBED_BATCH = 64               # texts per call of the injected embedder (bounded memory, progress in the log)
OLDER, NEWER = "older", "newer"
_LOG_EVERY = 25                # batches


class Neighbour(NamedTuple):
    """A sentence of the other section: its ``[start, end)`` section-text span and its cosine similarity to the query."""

    start: int
    end: int
    cosine: float


def embedder_name(embed: Callable[..., Any]) -> str:
    """The identity recorded in the cache index (``embed.name`` when it has one)."""
    return str(getattr(embed, "name", None) or "embedder")


def embed_texts(embed: EmbedFn, texts: Sequence[str]) -> np.ndarray:
    """``(len(texts), dim)`` float32, L2-normalised (so a cosine is a dot product), embedded ``EMBED_BATCH`` texts at a time."""
    cut = [t[:MAX_EMBED_CHARS] for t in texts]
    if not cut:
        return np.empty((0, 0), dtype=np.float32)
    parts = []
    for number, first in enumerate(range(0, len(cut), EMBED_BATCH), 1):
        batch = cut[first:first + EMBED_BATCH]
        out = np.asarray(embed(batch), dtype=np.float32)
        if out.ndim != 2 or out.shape[0] != len(batch):
            raise ValueError(f"the embedder returned shape {out.shape}: expected {len(batch)} rows of one vector each")
        parts.append(out)
        if number % _LOG_EVERY == 0:
            logger.info("embedded %d of %d sentence(s)", min(first + EMBED_BATCH, len(cut)), len(cut))
    matrix = np.vstack(parts)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1.0, norms)


class SectionMatrix:
    """The sentence spans of one section and their unit vectors (``vectors[i]`` belongs to ``spans[i]``)."""

    def __init__(self, spans: Sequence[tuple[int, int]], vectors: np.ndarray) -> None:
        self.spans = tuple((int(a), int(b)) for a, b in spans)
        self.vectors = vectors
        self._row = {span: i for i, span in enumerate(self.spans)}
        self._eligible: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    def row_of(self, span: tuple[int, int]) -> int | None:
        return self._row.get((int(span[0]), int(span[1])))

    def _eligible_rows(self, min_chars: int) -> tuple[np.ndarray, np.ndarray]:
        if min_chars not in self._eligible:
            rows = np.array([i for i, (a, b) in enumerate(self.spans) if b - a >= min_chars], dtype=np.int64)
            self._eligible[min_chars] = (rows, self.vectors[rows] if len(rows) else self.vectors[:0])
        return self._eligible[min_chars]

    def rank(self, query: np.ndarray, count: int, *, min_chars: int) -> list[Neighbour]:
        """The ``count`` sentences of at least ``min_chars`` characters closest to ``query``, best first; ties go to the earlier."""
        rows, vectors = self._eligible_rows(min_chars)
        if not len(rows) or count < 1:
            return []
        sims = vectors @ query
        order = np.argsort(-sims, kind="stable")[:count]
        return [Neighbour(*self.spans[int(rows[j])], float(sims[j])) for j in order]


# --------------------------------------------------------------------------
# the disk cache
# --------------------------------------------------------------------------

def cache_paths(cache_dir: Path, accession: str, text: str) -> tuple[Path, Path]:
    """``(npy, index json)`` of a section: named by the filing and the first 8 hex digits of the section text's content hash."""
    stem = f"{re.sub(r'[^0-9A-Za-z._-]', '_', accession)}_{content_hash(text)[:8]}"
    return cache_dir / f"{stem}.npy", cache_dir / f"{stem}.json"


def _index(name: str, text: str, spans: Sequence[tuple[int, int]], vectors: np.ndarray) -> dict:
    return {"version": CACHE_VERSION, "embedder": name, "section_sha": content_hash(text), "n": len(spans),
            "dim": int(vectors.shape[1]), "spans": [list(s) for s in spans]}


def _read_cache(npy: Path, js: Path, name: str, text: str, spans: Sequence[tuple[int, int]]) -> np.ndarray | None:
    """The cached vectors when the index and the array both describe exactly this text, spans and embedder; else None."""
    try:
        index = json.loads(js.read_text(encoding="utf-8"))
        vectors = np.load(npy, allow_pickle=False)
    except (OSError, ValueError, EOFError):
        return None
    if not isinstance(index, dict) or vectors.ndim != 2 or vectors.dtype != np.float32 or vectors.shape[0] != len(spans):
        return None
    return vectors if index == _index(name, text, spans, vectors) else None


def _write_cache(npy: Path, js: Path, index: dict, vectors: np.ndarray) -> None:
    """Array first, index last, both atomic. A failure leaves no partial file and is logged: the cache is derived data."""
    tmp_npy, tmp_js = npy.with_name(npy.name + ".tmp"), js.with_name(js.name + ".tmp")
    try:
        npy.parent.mkdir(parents=True, exist_ok=True)
        with tmp_npy.open("wb") as fh:
            np.save(fh, vectors, allow_pickle=False)
        os.replace(tmp_npy, npy)
        tmp_js.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")
        os.replace(tmp_js, js)
    except OSError as err:
        logger.warning("could not write the sentence-embedding cache %s (%s): the vectors are used from memory", npy.name, err)
    finally:
        tmp_npy.unlink(missing_ok=True)
        tmp_js.unlink(missing_ok=True)


def section_matrix(cache_dir: Path | None, accession: str, text: str, embed: EmbedFn) -> SectionMatrix:
    """The embedded sentences of one section text, from the cache when it is valid, else embedded (and cached).

    ``cache_dir=None`` keeps everything in memory. A text without a sentence has no rows and calls nothing."""
    spans = split_sentences(text)
    if not spans:
        return SectionMatrix((), np.empty((0, 0), dtype=np.float32))
    name = embedder_name(embed)
    npy = js = None
    if cache_dir is not None:
        npy, js = cache_paths(cache_dir, accession, text)
        cached = _read_cache(npy, js, name, text, spans)
        if cached is not None:
            return SectionMatrix(spans, cached)
    logger.info("embedding %d sentence(s) of %s", len(spans), accession)
    vectors = embed_texts(embed, [text[a:b] for a, b in spans])
    if npy is not None and js is not None:
        _write_cache(npy, js, _index(name, text, spans, vectors), vectors)
    return SectionMatrix(spans, vectors)


# --------------------------------------------------------------------------
# neighbours across a pair
# --------------------------------------------------------------------------

class PairNeighbours:
    """The sentences of the OTHER section of a pair closest to a sentence (module docstring).

    ``older`` / ``newer``: ``(accession, section text)``. Nothing is embedded until :meth:`neighbours` is called; a section is
    then read from the cache or embedded, once per object."""

    def __init__(self, older: tuple[str, str], newer: tuple[str, str], embed: EmbedFn, cache_dir: Path | None, *,
                 min_chars: int) -> None:
        self._sections = {OLDER: older, NEWER: newer}
        self._embed, self._cache_dir, self._min_chars = embed, cache_dir, min_chars
        self._matrices: dict[str, SectionMatrix] = {}
        self._loose: dict[str, np.ndarray] = {}

    def _matrix(self, side: str) -> SectionMatrix:
        if side not in self._matrices:
            accession, text = self._sections[side]
            self._matrices[side] = section_matrix(self._cache_dir, accession, text, self._embed)
        return self._matrices[side]

    def neighbours(self, side: str, start: int, end: int, text: str, count: int) -> list[Neighbour]:
        """The ``count`` sentences of the other section closest to ``text``, the sentence ``[start, end)`` of ``side``'s section."""
        if side not in self._sections:
            raise ValueError(f"side must be {OLDER!r} or {NEWER!r}, got {side!r}")
        own, other = self._matrix(side), self._matrix(NEWER if side == OLDER else OLDER)
        row = own.row_of((start, end))
        if row is not None:
            query = own.vectors[row]
        else:
            if text not in self._loose:
                self._loose[text] = embed_texts(self._embed, [text])[0]
            query = self._loose[text]
        return other.rank(query, count, min_chars=self._min_chars)


# --------------------------------------------------------------------------
# the adapter over the project's embedder
# --------------------------------------------------------------------------

class SentenceEmbedder:
    """``list[str] -> array``: sentences encoded as PASSAGES (no query instruction) by the project's ``Embedder``.

    Lazy: constructing it (and asking its ``name``) loads nothing; the model is loaded by the first call, whichever backend
    ``EMBEDDING_BACKEND`` selects (``local`` by default, the quickest for a batch: the ONNX backend encodes one text at a time).
    ``name`` is the backend and the configured model, which identify the vectors in the cache. ``batch_size`` 8 is the measured CPU optimum on the
    24-thread development machine (300 real sentences: 31 sentences/s at 8, 28 at 4 and 16, 18 at 32, 11 at 64: padding waste).
    ``embedder_factory`` is for tests."""

    def __init__(self, backend: str | None = None, batch_size: int = 8, *, embedder_factory: Callable[[], Any] | None = None) -> None:
        self._backend, self._batch_size, self._factory = backend, batch_size, embedder_factory
        self._embedder: Any = None

    @property
    def name(self) -> str:
        """``<backend>:<model>``: the vectors of the quantised ONNX export differ slightly from the local fp32 ones, so the two must
        not share a cache file."""
        from ..config import get_settings

        settings = get_settings()
        return f"{(self._backend or settings.embedding_backend).lower()}:{settings.embedding_model}"

    def __call__(self, texts: list[str]) -> np.ndarray:
        if self._embedder is None:
            if self._factory is not None:
                self._embedder = self._factory()
            else:
                from ..embeddings import Embedder

                self._embedder = Embedder(None, self._backend)
        return self._embedder.encode_passages(list(texts), batch_size=self._batch_size)

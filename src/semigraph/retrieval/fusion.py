"""Fusion of the dense excerpt legs (docs/specs/m2-search.md R6, R10): reciprocal rank fusion plus the slot policy.

Pure Python, no database. ``hybrid_retrieve`` (retriever.py) runs the enabled legs on one query vector and hands the ranked
rows here; the fused excerpts REPLACE the ``k_chunks`` slots, they never add to them.
"""

from collections.abc import Mapping, Sequence
import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .keyword import KeywordIndex

FUSION_VERSION = "i3a"       # part of the template fingerprint when RETRIEVAL_FUSION is on (never when off)
EXCERPT_MAX_CHARS = 12_000   # one fused excerpt's text is cut here: serve/estimate.CHUNK_TEXT_MAX_CHARS, pinned by a test (R12)
RRF_K = 60
FUSION_DEPTH = 20            # ranks of each leg that count in the fusion
SCORE_DIGITS = 12          # RRF scores are rounded to this many places before ties are broken by chunk id
RERANK_DEPTH = 20            # fused candidates the reranker scores (I3b)
LOG_DEPTH = 50               # ranks of each leg the bench may log (and the depth each leg is fetched to)


@dataclass(frozen=True)
class FusionConfig:
    """Which legs run and how deep. The mention leg is the existing (any-filer) mention query; ``filer`` and ``period`` run
    once per anchor."""
    mention: bool = True
    filer: bool = False
    period: bool = False
    keyword: bool = False            # bm25s legs: one per anchor (filer mask) and one mention-scoped leg (needs a KeywordIndex)
    keyword_context: bool = False    # the keyword index carries the context field (company, ticker, form, year, section)
    current_only: bool = True        # the filer, period and keyword legs keep only is_current rows (decision 011: in-index)
    mention_current_only: bool = False   # the mention leg too (decision 011, its own switch so it can be reverted alone)
    mention_candidates: int = 60     # ANN candidates before the MENTIONS filter (as EXCERPT_CANDIDATES today)
    filer_candidates: int = 100      # over-fetch inside the index for the filer leg (the filter is in-index, so rows fill)
    period_candidates: int = 100
    rerank: str | None = None        # a rerank model key (retrieval/rerank.py RERANK_MODELS) or None = no rerank stage (I3b)
    rerank_depth: int = RERANK_DEPTH
    rrf_k: int = RRF_K
    depth: int = FUSION_DEPTH
    log_depth: int = LOG_DEPTH

    @property
    def fetch_k(self) -> int:
        """Rows each leg returns: enough for both the fusion and the logged lists."""
        return max(self.depth, self.log_depth)


@dataclass(frozen=True)
class SlotPolicy:
    """R6: guaranteed filer-own slots per anchor (2 for 1-2 anchors, 1 each for 3-4), guaranteed mention slots, a cap per
    accession, dedup on content hash, never more than ``k_chunks`` slots."""
    k_chunks: int = 8
    mention_slots: int = 2
    max_per_accession: int = 3

    def filer_slots(self, n_anchors: int) -> int:
        return 2 if n_anchors <= 2 else 1


def rrf(lists: Mapping[str, Sequence[str]], *, k: int = RRF_K, depth: int = FUSION_DEPTH) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: ``(chunk_id, score)`` best first, ties broken by chunk id (deterministic)."""
    score: dict[str, float] = {}
    for ids in lists.values():
        for rank, cid in enumerate(dict.fromkeys(ids[:depth]), start=1):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + rank)
    # rounded so the same reciprocals summed in a different order (one ulp apart) still tie, and the id decides
    return sorted(((cid, round(s, SCORE_DIGITS)) for cid, s in score.items()), key=lambda kv: (-kv[1], kv[0]))


class _Slots:
    """The admitted candidates, with the dedup and accession-cap rules."""

    def __init__(self, policy: SlotPolicy, rows: Mapping[str, Mapping]):
        self.policy, self.rows = policy, rows
        self.ids: list[str] = []
        self.hashes: set[str] = set()
        self.per_accession: dict[str, int] = {}

    def offer(self, cid: str) -> bool:
        row = self.rows.get(cid)
        if row is None or cid in self.ids or len(self.ids) >= self.policy.k_chunks:
            return False
        digest, acc = row.get("content_hash"), row.get("accession_no")
        if digest and digest in self.hashes:
            return False
        if acc and self.per_accession.get(acc, 0) >= self.policy.max_per_accession:
            return False
        self.ids.append(cid)
        if digest:
            self.hashes.add(digest)
        if acc:
            self.per_accession[acc] = self.per_accession.get(acc, 0) + 1
        return True


def select_slots(fused: Sequence[tuple[str, float]], rows: Mapping[str, Mapping], *, filer_lists: Sequence[Sequence[str]],
                 mention_list: Sequence[str], policy: SlotPolicy) -> list[tuple[str, float]]:
    """Fill at most ``policy.k_chunks`` slots: the guaranteed filer-own and mention slots first (in leg order), then the
    fused order. Returns ``(chunk_id, rrf_score)`` best first (ties by chunk id)."""
    slots = _Slots(policy, rows)
    per_anchor = policy.filer_slots(len(filer_lists))
    for ids in filer_lists:
        taken = 0
        for cid in ids:
            if taken >= per_anchor:
                break
            taken += slots.offer(cid)
    taken = 0
    for cid in mention_list:
        if taken >= policy.mention_slots:
            break
        taken += slots.offer(cid)
    for cid, _ in fused:
        slots.offer(cid)
    score = dict(fused)
    return sorted(((cid, score.get(cid, 0.0)) for cid in slots.ids), key=lambda kv: (-kv[1], kv[0]))


# What the API serves with RETRIEVAL_FUSION on: the legs of the bench's ``all`` arm (mention + filer + period + keyword with the
# context field), so the bench measures what is served. Not yet trimmed by the ship rule per component. ``mention_current_only``
# stays off: the mention leg is unchanged until decision 011's mention change lands as its own commit (set it here and in the
# bench arms together).
SERVING_CONFIG = FusionConfig(filer=True, period=True, keyword=True, keyword_context=True)


@dataclass(frozen=True)
class FusionRuntime:
    """What the serving paths hand to ``hybrid_retrieve`` when the flag is on and the keyword index was built: the leg
    configuration and the in-memory index (keyed by the graph snapshot it was built from)."""
    config: FusionConfig
    index: "KeywordIndex"

    @property
    def snapshot_id(self) -> str:
        return self.index.snapshot_id


def serving_config(settings) -> FusionConfig:
    """``SERVING_CONFIG`` plus the rerank stage when ``RETRIEVAL_RERANK`` names a model whose files load (default none:
    unchanged; a missing or broken model gives the config without the stage, so the fingerprint and the cache keys never claim
    a rerank that does not run)."""
    key = getattr(settings, "retrieval_rerank", "none")
    if key == "none":
        return SERVING_CONFIG
    from . import rerank

    if rerank.get_reranker(key) is None:      # loads (warms) the model once; a missing one means no stage, so no "rerank" in the key
        return SERVING_CONFIG
    return replace(SERVING_CONFIG, rerank=key, rerank_depth=RERANK_DEPTH)


def serving_digest() -> str:
    """A short hash of the serving config (``SERVING_CONFIG`` with the rerank setting applied): part of the fingerprint, so
    trimming a leg, flipping a switch or turning the reranker on can never replay answers cached under the old legs without a
    ``FUSION_VERSION`` bump."""
    from ..config import get_settings

    return hashlib.sha256(repr(serving_config(get_settings())).encode("utf-8")).hexdigest()[:8]


def sec_fusion_kwargs(runtime: "FusionRuntime | None", as_of: str | None = None) -> dict:
    """The ``hybrid_retrieve`` keywords for the SEC part of a workspace ask. None means ``{}`` (today's path). An ``as_of`` ask
    never uses the current-only keyword index (spec OD8): it keeps the dense legs and drops the keyword legs."""
    if runtime is None:
        return {}
    config = runtime.config                            # never the reranker on the workspace path (I3b)
    if config.rerank:
        config = replace(config, rerank=None)
    if as_of:
        return {"fusion": replace(config, keyword=False), "keyword_index": None}
    return {"fusion": config, "keyword_index": runtime.index}

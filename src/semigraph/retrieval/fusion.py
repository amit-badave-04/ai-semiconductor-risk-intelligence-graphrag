"""Fusion of the dense excerpt legs (docs/specs/m2-search.md R6, R10): reciprocal rank fusion plus the slot policy.

Pure Python, no database. ``hybrid_retrieve`` (retriever.py) runs the enabled legs on one query vector and hands the ranked
rows here; the fused excerpts REPLACE the ``k_chunks`` slots, they never add to them.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

FUSION_VERSION = "i2"        # part of the template fingerprint once the flag is wired (I3)
RRF_K = 60
FUSION_DEPTH = 20            # ranks of each leg that count in the fusion
SCORE_DIGITS = 12          # RRF scores are rounded to this many places before ties are broken by chunk id
LOG_DEPTH = 50               # ranks of each leg the bench may log (and the depth each leg is fetched to)


@dataclass(frozen=True)
class FusionConfig:
    """Which legs run and how deep. The mention leg is the existing (any-filer) mention query; ``filer`` and ``period`` run
    once per anchor."""
    mention: bool = True
    filer: bool = False
    period: bool = False
    mention_candidates: int = 60     # ANN candidates before the MENTIONS filter (as EXCERPT_CANDIDATES today)
    filer_candidates: int = 100      # over-fetch inside the index for the filer leg (the filter is in-index, so rows fill)
    period_candidates: int = 100
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

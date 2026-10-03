"""The embedder as the serving layer uses it (M5a I2, docs/v2/M5A_BUILD_PLAN.md section 2).

Query embedding is CPU-bound (0.3 s to 1.2 s of a core), so two things matter under concurrent streams: how many run at
once, and not doing the same one twice. :class:`LimitedEmbedder` wraps the real embedder with

* ONE ``threading.BoundedSemaphore(slots)`` taken by every caller of ``encode_query``: the async answer path (which calls it
  from a worker thread) and the agent's tools (which call it from the planner thread) share the same bound;
* a bounded LRU of query vectors, stored as float32 bytes (the backends return float32 vectors, so this is lossless, and
  2,000 vectors of 1,024 dimensions are about 8 MB, where lists of Python floats would be about 65 MB). The cache is read
  again after the slot is taken, so concurrent copies of one question wait for the first one's vector instead of each
  computing their own.

Nothing else is changed: passages, token counts and every other attribute are the wrapped embedder's own and take no slot
(uploads embed passages under their own one-at-a-time gate).
"""

import threading
from collections import OrderedDict

import numpy as np

DEFAULT_CACHE_SIZE = 2_000


class LimitedEmbedder:
    def __init__(self, embedder, slots: int, cache_size: int = DEFAULT_CACHE_SIZE):
        if slots < 1:
            raise ValueError(f"slots must be 1 or more, got {slots}")
        if cache_size < 1:
            raise ValueError(f"cache_size must be 1 or more, got {cache_size}")
        self._embedder = embedder
        self._slots = threading.BoundedSemaphore(slots)
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def _cached(self, question: str) -> list[float] | None:
        with self._lock:
            raw = self._cache.get(question)
            if raw is None:
                return None
            self._cache.move_to_end(question)
        return np.frombuffer(raw, dtype=np.float32).tolist()

    def _store(self, question: str, vector: list[float]) -> None:
        raw = np.asarray(vector, dtype=np.float32).tobytes()
        with self._lock:
            self._cache[question] = raw
            self._cache.move_to_end(question)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def encode_query(self, question: str) -> list[float]:
        hit = self._cached(question)
        if hit is not None:
            return hit
        with self._slots:
            hit = self._cached(question)  # another thread may have embedded it while this one waited for the slot
            if hit is not None:
                return hit
            vector = self._embedder.encode_query(question)
            self._store(question, vector)
            return vector

    def __getattr__(self, name: str):
        # Only reached for names this class does not define: delegate, but never loop on the wrapped attribute itself.
        if name == "_embedder":
            raise AttributeError(name)
        return getattr(self._embedder, name)

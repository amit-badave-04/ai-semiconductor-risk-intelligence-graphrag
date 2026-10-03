"""The serving embedder wrapper (M5a I2): ONE concurrency bound over every caller and a bounded cache of query vectors."""

import threading
import time

import numpy as np
import pytest

from semigraph.serve.embed import LimitedEmbedder


class FakeInner:
    """An embedder whose encode_query is slow and records how many calls overlapped."""
    name = "fake-embedder"
    backend = "fake"

    def __init__(self, delay: float = 0.0, fail_on: set[str] | None = None):
        self.delay, self.fail_on = delay, fail_on or set()
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self.running = self.max_running = 0

    def encode_query(self, question: str) -> list[float]:
        with self._lock:
            self.calls.append(question)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            time.sleep(self.delay)
            if question in self.fail_on:
                raise RuntimeError("embedder failed")
            seed = sum(question.encode()) % 997
            return np.random.default_rng(seed).standard_normal(8).astype(np.float32).tolist()
        finally:
            with self._lock:
                self.running -= 1

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def encode_passages(self, texts, batch_size=8, show_progress=False):
        return np.zeros((len(texts), 8), dtype=np.float32)


def test_a_repeated_question_is_served_from_the_cache_with_the_same_vector():
    inner = FakeInner()
    emb = LimitedEmbedder(inner, slots=1)

    first, second = emb.encode_query("What is HBM?"), emb.encode_query("What is HBM?")

    assert second == first and inner.calls == ["What is HBM?"]


def test_cached_vectors_equal_what_the_embedder_returned_exactly():
    """float32 storage is lossless for the float32 vectors the ONNX and local backends return."""
    inner = FakeInner()
    emb = LimitedEmbedder(inner, slots=1)
    direct = inner.encode_query("q")

    emb.encode_query("q")

    assert emb.encode_query("q") == direct and all(isinstance(x, float) for x in emb.encode_query("q"))


def test_different_questions_are_embedded_separately():
    inner = FakeInner()
    emb = LimitedEmbedder(inner, slots=1)

    a, b = emb.encode_query("one"), emb.encode_query("two")

    assert a != b and inner.calls == ["one", "two"]


def test_the_cache_is_bounded_and_evicts_the_least_recently_used_question():
    inner = FakeInner()
    emb = LimitedEmbedder(inner, slots=1, cache_size=2)
    emb.encode_query("a")
    emb.encode_query("b")
    emb.encode_query("a")  # a is now the most recent
    emb.encode_query("c")  # evicts b

    inner.calls.clear()
    emb.encode_query("a")
    emb.encode_query("c")
    assert inner.calls == []
    emb.encode_query("b")
    assert inner.calls == ["b"]


def test_the_slot_count_bounds_concurrent_embeddings_across_threads():
    inner = FakeInner(delay=0.05)
    emb = LimitedEmbedder(inner, slots=2)
    threads = [threading.Thread(target=emb.encode_query, args=(f"question {i}",)) for i in range(8)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert inner.max_running == 2 and len(inner.calls) == 8


def test_concurrent_identical_questions_are_embedded_once():
    """The cache is re-checked after the slot is taken: 20 waiting copies of one question cost one embedding."""
    inner = FakeInner(delay=0.05)
    emb = LimitedEmbedder(inner, slots=1)
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(emb.encode_query("same"))) for _ in range(20)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert inner.calls == ["same"] and len(results) == 20 and all(r == results[0] for r in results)


def test_a_failing_embedding_is_not_cached_and_releases_the_slot():
    inner = FakeInner(fail_on={"boom"})
    emb = LimitedEmbedder(inner, slots=1)

    with pytest.raises(RuntimeError, match="embedder failed"):
        emb.encode_query("boom")
    with pytest.raises(RuntimeError, match="embedder failed"):
        emb.encode_query("boom")

    assert inner.calls == ["boom", "boom"]
    assert emb.encode_query("fine")  # the slot is free again


def test_every_other_attribute_comes_from_the_wrapped_embedder():
    inner = FakeInner()
    emb = LimitedEmbedder(inner, slots=1)

    assert emb.name == "fake-embedder" and emb.backend == "fake"
    assert emb.count_tokens("one two three") == 3
    assert emb.encode_passages(["x", "y"]).shape == (2, 8)


def test_a_missing_attribute_raises_attribute_error():
    emb = LimitedEmbedder(FakeInner(), slots=1)

    with pytest.raises(AttributeError):
        emb.no_such_attribute  # noqa: B018


@pytest.mark.parametrize("slots,size", [(0, 10), (-1, 10), (1, 0), (1, -5)])
def test_invalid_limits_are_refused(slots, size):
    with pytest.raises(ValueError):
        LimitedEmbedder(FakeInner(), slots=slots, cache_size=size)

"""Per-chunk embedding cache tests — a fake encoder, no model, no network.

The cache file format (columns ``chunk_id, embedding``) is shared with the
notebook-built caches, so existing caches must stay valid; only chunk_ids
missing from the cache may ever be encoded.
"""

import hashlib
import logging

import numpy as np
import pandas as pd
import pytest

from semigraph.embeddings import Embedder, embed_chunks_cached

DIM = 8


def vec_for(text: str) -> np.ndarray:
    """Deterministic unit vector derived from the text (float32)."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


class FakeEncoder:
    """Stands in for ``Embedder.encode_passages``; records every call."""

    def __init__(self, salt: str = ""):
        self.salt = salt
        self.calls: list[dict] = []

    def __call__(self, texts, batch_size=8, show_progress=False):
        self.calls.append(
            {"texts": list(texts), "batch_size": batch_size, "show_progress": show_progress}
        )
        return np.vstack([vec_for(self.salt + t) for t in texts]) if texts else np.empty((0, DIM), np.float32)

    @property
    def encoded_texts(self) -> list[str]:
        return [t for c in self.calls for t in c["texts"]]


def make_chunks(n: int, start: int = 0, with_heading: bool = True) -> pd.DataFrame:
    return pd.DataFrame({
        "chunk_id": [f"ACC:I.1A:{i:04d}" for i in range(start, start + n)],
        "sub_heading": [f"Heading {i}" if (with_heading and i % 2 == 0) else None
                        for i in range(start, start + n)],
        "text": [f"Body text of chunk {i}." for i in range(start, start + n)],
    })


def read_cache(path) -> pd.DataFrame:
    return pd.read_parquet(path)


class TestFirstBuild:
    def test_no_cache_encodes_everything_and_writes_the_cache(self, tmp_path):
        chunks, enc, path = make_chunks(5), FakeEncoder(), tmp_path / "emb" / "T_chunk_embeddings.parquet"

        out = embed_chunks_cached(chunks, path, enc)

        assert out.shape == (5, DIM)
        assert len(enc.encoded_texts) == 5
        cache = read_cache(path)
        assert list(cache.columns) == ["chunk_id", "embedding"]  # legacy-compatible format
        assert cache["chunk_id"].tolist() == chunks["chunk_id"].tolist()
        np.testing.assert_array_equal(np.vstack(cache["embedding"].to_numpy()), out)

    def test_embed_input_is_subheading_newline_text_stripped(self, tmp_path):
        chunks, enc = make_chunks(2), FakeEncoder()
        embed_chunks_cached(chunks, tmp_path / "c.parquet", enc)
        assert enc.encoded_texts == [
            "Heading 0\nBody text of chunk 0.",   # heading present
            "Body text of chunk 1.",              # no heading: leading newline stripped
        ]

    def test_batch_size_and_progress_are_forwarded(self, tmp_path):
        enc = FakeEncoder()
        embed_chunks_cached(make_chunks(3), tmp_path / "c.parquet", enc, batch_size=32)
        assert enc.calls[0]["batch_size"] == 32 and enc.calls[0]["show_progress"] is True

    def test_empty_chunks_returns_empty_array_and_never_encodes(self, tmp_path):
        enc, path = FakeEncoder(), tmp_path / "c.parquet"
        out = embed_chunks_cached(make_chunks(0), path, enc)
        assert out.shape[0] == 0 and enc.calls == []
        assert not path.exists()


class TestCacheHit:
    def test_full_cache_hit_encodes_nothing_and_leaves_the_file_alone(self, tmp_path):
        chunks, path = make_chunks(6), tmp_path / "c.parquet"
        first = embed_chunks_cached(chunks, path, FakeEncoder())
        before = path.read_bytes()
        enc = FakeEncoder()

        second = embed_chunks_cached(chunks, path, enc)

        assert enc.calls == []
        np.testing.assert_array_equal(second, first)
        assert path.read_bytes() == before  # not even rewritten

    def test_reads_a_cache_in_the_legacy_notebook_layout(self, tmp_path):
        chunks, path = make_chunks(4), tmp_path / "nvda_chunk_embeddings.parquet"
        legacy = np.vstack([vec_for(f"legacy-{i}") for i in range(4)])
        pd.DataFrame({"chunk_id": chunks["chunk_id"], "embedding": list(legacy)}).to_parquet(
            path, index=False
        )
        enc = FakeEncoder()

        out = embed_chunks_cached(chunks, path, enc)

        assert enc.calls == []
        np.testing.assert_array_equal(out, legacy)


class TestIncrementalEmbedding:
    def test_adding_one_chunk_encodes_exactly_one(self, tmp_path):
        path = tmp_path / "c.parquet"
        embed_chunks_cached(make_chunks(5), path, FakeEncoder())
        enc = FakeEncoder()

        out = embed_chunks_cached(make_chunks(6), path, enc)

        assert enc.encoded_texts == ["Body text of chunk 5."]  # only the new chunk (odd id: no heading)
        assert out.shape == (6, DIM)
        assert len(read_cache(path)) == 6

    def test_existing_vectors_are_returned_bit_identical(self, tmp_path):
        path = tmp_path / "c.parquet"
        original = embed_chunks_cached(make_chunks(5), path, FakeEncoder())
        # a DIFFERENT encoder (think: another library build) must not perturb old vectors
        out = embed_chunks_cached(make_chunks(8), path, FakeEncoder(salt="drift-"))

        assert out[:5].tobytes() == original.tobytes()
        assert out.dtype == original.dtype
        cached_again = np.vstack(read_cache(path)["embedding"].to_numpy())
        assert cached_again[:5].tobytes() == original.tobytes()

    def test_result_order_follows_chunks_df_even_when_new_chunks_sit_in_the_middle(self, tmp_path):
        path = tmp_path / "c.parquet"
        base = make_chunks(4)
        embed_chunks_cached(base, path, FakeEncoder())
        new = make_chunks(1, start=99)
        chunks = pd.concat([base.iloc[:2], new, base.iloc[2:]], ignore_index=True)
        enc = FakeEncoder()

        out = embed_chunks_cached(chunks, path, enc)

        assert len(enc.encoded_texts) == 1
        cache = read_cache(path)
        by_id = dict(zip(cache["chunk_id"], cache["embedding"]))
        for row, cid in zip(out, chunks["chunk_id"]):
            np.testing.assert_array_equal(row, by_id[cid])
        # new id really landed in position 2
        np.testing.assert_array_equal(out[2], by_id[new["chunk_id"].iloc[0]])

    def test_reordered_request_returns_rows_in_the_requested_order(self, tmp_path):
        path = tmp_path / "c.parquet"
        chunks = make_chunks(5)
        forward = embed_chunks_cached(chunks, path, FakeEncoder())
        enc = FakeEncoder()

        backward = embed_chunks_cached(chunks.iloc[::-1].reset_index(drop=True), path, enc)

        assert enc.calls == []
        np.testing.assert_array_equal(backward, forward[::-1])

    def test_merged_cache_keeps_entries_no_longer_requested_and_appends_new_ones(self, tmp_path):
        path = tmp_path / "c.parquet"
        embed_chunks_cached(make_chunks(5), path, FakeEncoder())
        # request only chunks 3..6: 3,4 cached; 5,6 new; 0,1,2 absent from this request
        embed_chunks_cached(make_chunks(4, start=3), path, FakeEncoder())

        ids = read_cache(path)["chunk_id"].tolist()
        assert ids == [f"ACC:I.1A:{i:04d}" for i in range(7)]  # old order kept, new appended

    def test_duplicate_chunk_ids_are_encoded_once(self, tmp_path):
        chunks = pd.concat([make_chunks(2), make_chunks(1)], ignore_index=True)  # id 0 twice
        enc = FakeEncoder()

        out = embed_chunks_cached(chunks, tmp_path / "c.parquet", enc)

        assert len(enc.encoded_texts) == 2
        assert out.shape == (3, DIM)
        np.testing.assert_array_equal(out[0], out[2])
        assert len(read_cache(tmp_path / "c.parquet")) == 2

    def test_index_of_chunks_df_is_irrelevant(self, tmp_path):
        chunks = make_chunks(4)
        chunks.index = [10, 3, 7, 1]
        enc = FakeEncoder()
        out = embed_chunks_cached(chunks, tmp_path / "c.parquet", enc)
        assert out.shape == (4, DIM)
        assert enc.encoded_texts[0].endswith("Body text of chunk 0.")


class TestBadCachesAndEncoders:
    def test_corrupt_cache_is_rebuilt_with_a_warning(self, tmp_path, caplog):
        path = tmp_path / "c.parquet"
        path.write_bytes(b"this is not a parquet file")
        enc = FakeEncoder()

        with caplog.at_level(logging.WARNING, logger="semigraph.embeddings"):
            out = embed_chunks_cached(make_chunks(3), path, enc)

        assert out.shape == (3, DIM) and len(enc.encoded_texts) == 3
        assert any("unreadable" in r.message for r in caplog.records)
        assert len(read_cache(path)) == 3  # healed

    def test_cache_with_wrong_columns_is_rebuilt_with_a_warning(self, tmp_path, caplog):
        path = tmp_path / "c.parquet"
        pd.DataFrame({"foo": [1, 2]}).to_parquet(path, index=False)
        with caplog.at_level(logging.WARNING, logger="semigraph.embeddings"):
            out = embed_chunks_cached(make_chunks(2), path, FakeEncoder())
        assert out.shape == (2, DIM)
        assert any("unreadable" in r.message for r in caplog.records)

    def test_encoder_returning_the_wrong_row_count_fails_and_keeps_the_cache(self, tmp_path):
        path = tmp_path / "c.parquet"
        embed_chunks_cached(make_chunks(3), path, FakeEncoder())
        before = path.read_bytes()

        def short_encoder(texts, batch_size=8, show_progress=False):
            return np.zeros((len(texts) - 1, DIM), dtype=np.float32)

        with pytest.raises(ValueError, match="rows"):
            embed_chunks_cached(make_chunks(5), path, short_encoder)
        assert path.read_bytes() == before

    def test_float64_encoder_output_is_stored_and_returned_as_float32(self, tmp_path):
        """A first call and a later cache hit must return identical values,
        even for a backend that emits float64."""
        path = tmp_path / "c.parquet"

        def f64_encoder(texts, batch_size=8, show_progress=False):
            return np.vstack([vec_for(t) for t in texts]).astype(np.float64) * 1.0000001

        first = embed_chunks_cached(make_chunks(3), path, f64_encoder)
        second = embed_chunks_cached(make_chunks(3), path, FakeEncoder())

        assert first.dtype == np.float32
        assert first.tobytes() == second.tobytes()

    def test_no_temp_file_is_left_behind(self, tmp_path):
        embed_chunks_cached(make_chunks(3), tmp_path / "c.parquet", FakeEncoder())
        assert [p.name for p in tmp_path.iterdir()] == ["c.parquet"]


class TestEmbedderFacade:
    def test_encode_chunks_cached_method_uses_the_backend_and_only_missing_ids(self, tmp_path):
        """The public method (used by graph loaders) delegates to the same
        per-chunk logic via ``encode_passages`` — no model is loaded."""

        class StubBackend:
            def __init__(self):
                self.encoder = FakeEncoder()
                self.name = "stub"

            def encode_passages(self, texts, batch_size=8, show_progress=False):
                return self.encoder(texts, batch_size=batch_size, show_progress=show_progress)

        embedder = Embedder.__new__(Embedder)  # bypass __init__: it would load a model
        embedder._impl = StubBackend()
        path = tmp_path / "c.parquet"

        first = embedder.encode_chunks_cached(make_chunks(4), path)
        assert len(embedder._impl.encoder.encoded_texts) == 4
        second = embedder.encode_chunks_cached(make_chunks(5), path)

        assert len(embedder._impl.encoder.encoded_texts) == 5  # +1 only
        np.testing.assert_array_equal(second[:4], first)

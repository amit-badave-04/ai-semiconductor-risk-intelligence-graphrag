"""M4: ``Embedder.count_tokens``, the embedder's own tokenizer count behind the upload caps (docs/v2/M4_PLAN.md section 3).

The per-version cap (16,000 tokens, whole text) and the per-chunk cap (512 tokens) must be counted with the tokenizer the
embedding model really uses. The ONNX tokenizer is configured to TRUNCATE at 8,192 tokens (it bounds embedding memory), so a
whole-document count in one call would silently cap at 8,192 and let an over-size upload through: counting goes through pieces
far below the truncation limit.
"""

from pathlib import Path

import pytest

from semigraph import embeddings
from semigraph.embeddings_onnx import MAX_TOKENS, OnnxBackend

TOKENIZER = Path(__file__).resolve().parents[1] / "models" / "qwen3-embedding-0.6b-q8" / "tokenizer.json"
LONG = "Export controls restrict sales of advanced computing products to certain customers. " * 2500


class WordImpl:
    name = "fake"

    def count_tokens(self, text: str) -> int:
        return len(text.split())


def _embedder(impl) -> embeddings.Embedder:
    e = object.__new__(embeddings.Embedder)
    e._impl, e.name, e.backend = impl, getattr(impl, "name", "x"), "fake"
    return e


def test_the_facade_counts_with_its_backend():
    assert _embedder(WordImpl()).count_tokens("a b c") == 3


def test_a_backend_without_a_tokenizer_cannot_count():
    with pytest.raises(NotImplementedError):
        _embedder(object()).count_tokens("a b c")


@pytest.mark.parametrize("text", ["", "x", "one two", LONG, "é" * 5000, "no-spaces-" * 900],
                         ids=["empty", "one-char", "two-words", "long-prose", "multibyte", "no-spaces"])
def test_pieces_cover_the_text_exactly_and_stay_small(text):
    pieces = embeddings.split_for_counting(text)
    assert "".join(pieces) == text
    assert all(0 < len(p) <= embeddings.COUNT_PIECE_CHARS for p in pieces)


@pytest.mark.skipif(not TOKENIZER.exists(), reason="the ONNX tokenizer is built locally (scripts/build_onnx_embedder.py)")
def test_a_long_document_is_counted_past_the_truncation_limit_with_the_real_tokenizer():
    from tokenizers import Tokenizer

    truncating = Tokenizer.from_file(str(TOKENIZER))
    truncating.enable_truncation(MAX_TOKENS)
    backend = object.__new__(OnnxBackend)
    backend.tokenizer = truncating
    exact = len(Tokenizer.from_file(str(TOKENIZER)).encode(LONG).ids)
    counted = backend.count_tokens(LONG)
    assert len(truncating.encode(LONG).ids) == MAX_TOKENS < exact          # the trap this guards against
    assert abs(counted - exact) <= exact * 0.005                           # piece boundaries sit on spaces
    assert backend.count_tokens("Nvidia depends on TSMC.") == len(truncating.encode("Nvidia depends on TSMC.").ids)

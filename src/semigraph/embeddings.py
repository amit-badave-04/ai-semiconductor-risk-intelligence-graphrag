"""Embeddings — Qwen3-Embedding-0.6B, 1024-dim (schema-locked), three backends.

``Embedder`` is a facade; ``settings.embedding_backend`` (or the ``backend``
argument) selects the implementation:

- ``local``  — sentence-transformers + torch. The pipeline default (notebooks,
  ``semigraph build-graph``). Conventions verified in notebooks 09/10/14: load
  from the local Hugging Face cache first (``local_files_only=True``), passages
  are embedded WITHOUT a prompt, queries get the model's "query" prompt.
- ``onnx``   — torch-free onnxruntime session over a quantized export built by
  ``scripts/build_onnx_embedder.py`` (what the web service image ships; that
  script verifies fidelity against ``local`` before it writes the file).
- ``remote`` — any OpenAI-compatible ``/embeddings`` endpoint serving the SAME
  model (the query instruction is prepended client-side).

All backends produce L2-normalized vectors in one space, so a query embedded
by any of them is comparable with passages embedded by ``local``. Embedding
caches are keyed by the exact chunk_id sequence — any change re-encodes.
"""

import logging
from pathlib import Path

import numpy as np

from .config import get_settings

logger = logging.getLogger("semigraph.embeddings")

EMBED_DIM = 1024

# The model card's query instruction — identical to the sentence-transformers
# "query" prompt in the model's config_sentence_transformers.json.
QUERY_PROMPT = ("Instruct: Given a web search query, retrieve relevant passages "
                "that answer the query\nQuery:")

BACKENDS = ("local", "onnx", "remote")


class LocalBackend:
    """sentence-transformers backend (the original notebook implementation)."""

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer

        try:
            self.model = SentenceTransformer(model_name, local_files_only=True)
        except OSError:
            logger.info("%s not in the local cache — downloading once", model_name)
            self.model = SentenceTransformer(model_name)
        self.name = model_name

    def encode_passages(self, texts: list[str], batch_size: int = 8,
                        show_progress: bool = False) -> np.ndarray:
        return self.model.encode(
            texts, batch_size=batch_size, show_progress_bar=show_progress,
            normalize_embeddings=True,
        )

    def encode_query(self, question: str) -> list[float]:
        if "query" in (self.model.prompts or {}):
            return self.model.encode(
                [question], prompt_name="query", normalize_embeddings=True
            )[0].tolist()
        return self.model.encode([question], normalize_embeddings=True)[0].tolist()


def _make_backend(backend: str, model_name: str | None):
    settings = get_settings()
    if backend == "local":
        return LocalBackend(model_name or settings.embedding_model)
    if backend == "onnx":
        from .embeddings_onnx import OnnxBackend
        return OnnxBackend(settings.onnx_model_path, settings.onnx_tokenizer_path,
                           threads=settings.onnx_threads)
    if backend == "remote":
        from .embeddings_remote import RemoteBackend
        return RemoteBackend(settings.embedding_api_base, settings.embedding_api_key,
                             settings.embedding_api_model)
    raise ValueError(f"unknown embedding backend {backend!r} — use one of {BACKENDS}")


class Embedder:
    """Facade over the configured backend (see module docstring)."""

    def __init__(self, model_name: str | None = None, backend: str | None = None):
        self.backend = (backend or get_settings().embedding_backend).lower()
        self._impl = _make_backend(self.backend, model_name)
        self.name = self._impl.name
        logger.info("embedder ready — backend=%s model=%s", self.backend, self.name)

    @property
    def model(self):
        """The underlying sentence-transformers model (``local`` backend only)."""
        return getattr(self._impl, "model", None)

    def encode_passages(self, texts: list[str], batch_size: int = 8,
                        show_progress: bool = False) -> np.ndarray:
        return self._impl.encode_passages(texts, batch_size=batch_size,
                                          show_progress=show_progress)

    def encode_query(self, question: str) -> list[float]:
        return self._impl.encode_query(question)

    def encode_chunks_cached(self, chunks_df, cache_path: Path,
                             batch_size: int = 8) -> np.ndarray:
        """Embed chunk rows (sub_heading + text), reusing the parquet cache
        when the chunk_id sequence is unchanged."""
        import pandas as pd  # pipeline-only dependency, kept out of the serving path

        if cache_path.exists():
            cached = pd.read_parquet(cache_path)
            if list(cached["chunk_id"]) == list(chunks_df["chunk_id"]):
                logger.info("reusing cached embeddings for %d chunks (%s)",
                            len(cached), cache_path.name)
                return np.vstack(cached["embedding"].to_numpy())
            logger.info("chunk set changed since cache was written — re-encoding")
        embed_input = (
            chunks_df["sub_heading"].fillna("") + "\n" + chunks_df["text"]
        ).str.strip()
        embeddings = self.encode_passages(
            embed_input.tolist(), batch_size=batch_size, show_progress=True
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {"chunk_id": chunks_df["chunk_id"], "embedding": list(embeddings)}
        ).to_parquet(cache_path, index=False)
        logger.info("computed and cached embeddings -> %s", cache_path)
        return embeddings

"""Local embeddings (Qwen3-Embedding-0.6B, 1024-dim — schema-locked).

Conventions verified in notebooks 09/10/14:
- load from the local Hugging Face cache first (local_files_only=True);
  download only if truly absent
- passages/documents are embedded WITHOUT a prompt; only queries get the
  model's "query" prompt (prompt_name="query") when the model defines one
- embedding caches are keyed by the exact chunk_id sequence — any change
  re-encodes
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .config import get_settings

logger = logging.getLogger("semigraph.embeddings")

EMBED_DIM = 1024


class Embedder:
    def __init__(self, model_name: str | None = None):
        from sentence_transformers import SentenceTransformer

        name = model_name or get_settings().embedding_model
        try:
            self.model = SentenceTransformer(name, local_files_only=True)
        except OSError:
            logger.info("%s not in the local cache — downloading once", name)
            self.model = SentenceTransformer(name)
        self.name = name

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

    def encode_chunks_cached(self, chunks_df: pd.DataFrame, cache_path: Path,
                             batch_size: int = 8) -> np.ndarray:
        """Embed chunk rows (sub_heading + text), reusing the parquet cache
        when the chunk_id sequence is unchanged."""
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

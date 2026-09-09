"""OpenAI-compatible remote embeddings backend.

For providers that serve exactly ``Qwen/Qwen3-Embedding-0.6B`` behind a
``POST {base}/embeddings`` endpoint (DeepInfra, SiliconFlow, Nebius, a
self-hosted TEI/vLLM box ...). The provider does NOT add the query
instruction, so this backend prepends ``QUERY_PROMPT`` itself — the same text
the local sentence-transformers backend prepends — keeping queries and the
pre-embedded passages in one space. Vectors are re-normalized defensively.
"""

import logging

import numpy as np

logger = logging.getLogger("semigraph.embeddings.remote")

BATCH = 32


class RemoteBackend:
    def __init__(self, api_base: str, api_key: str, model: str, timeout: float = 30.0):
        import httpx

        from .embeddings import EMBED_DIM, QUERY_PROMPT

        if not api_base or not api_key:
            raise RuntimeError(
                "EMBEDDING_API_BASE and EMBEDDING_API_KEY are required for the remote backend")
        self._client = httpx.Client(base_url=api_base.rstrip("/"), timeout=timeout,
                                    headers={"Authorization": f"Bearer {api_key}"})
        self._prompt = QUERY_PROMPT
        self._dim = EMBED_DIM
        self.model_name = model
        self.name = f"remote:{model}"

    def _embed(self, texts: list[str]) -> np.ndarray:
        resp = self._client.post("/embeddings", json={
            "model": self.model_name, "input": texts, "encoding_format": "float"})
        resp.raise_for_status()
        rows = sorted(resp.json()["data"], key=lambda d: d["index"])
        vecs = np.array([r["embedding"] for r in rows], dtype=np.float32)
        if vecs.shape != (len(texts), self._dim):
            raise RuntimeError(
                f"remote embeddings have shape {vecs.shape}, expected ({len(texts)}, {self._dim})")
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)

    def encode_passages(self, texts: list[str], batch_size: int = BATCH,
                        show_progress: bool = False) -> np.ndarray:
        return np.vstack([self._embed(texts[i:i + batch_size])
                          for i in range(0, len(texts), batch_size)])

    def encode_query(self, question: str) -> list[float]:
        return self._embed([self._prompt + question])[0].tolist()

"""A deterministic fake sentence embedder for the semantic-candidate tests (no model, no network).

``make_embed(groups)`` maps every word to a coordinate (words of one group share a coordinate: a stand-in for synonyms, so two
sentences that share almost no WORDS can still be close), counts the words, and L2-normalises. It records every call in
``embed.calls`` so tests can assert "nothing was embedded again".
"""

import zlib
from collections.abc import Sequence

import numpy as np

from semigraph.graph.align_text import word_tokens

DIM = 96

# Synonym groups that make the below-zone fixtures (test_passage_bands.BELOW / BELOW2) semantically close.
GROUPS = (
    ("weather", "disasters", "earthquakes", "natural"), ("suppliers", "vendors", "factories"),
    ("deliveries", "shipments"), ("interrupt", "halt"), ("component", "parts"), ("quarters", "months"),
    ("patent", "intellectual", "property"), ("infringement", "sue", "lawsuits"), ("competitors", "rivals"),
    ("settlements", "resolving"), ("costly", "expensive"), ("redesigns", "change"), ("product", "products"),
)


def make_embed(groups: Sequence[Sequence[str]] = GROUPS, *, name: str = "fake-embedder"):
    slot = {word: i for i, group in enumerate(groups) for word in group}

    def embed(texts: list[str]) -> np.ndarray:
        embed.calls.append(list(texts))
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in word_tokens(text):
                idx = slot.get(word, len(groups) + zlib.crc32(word.encode()) % (DIM - len(groups)))
                out[row, idx] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1.0, norms)

    embed.calls = []
    embed.name = name
    embed.embedded = lambda: [t for batch in embed.calls for t in batch]
    return embed

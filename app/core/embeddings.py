"""Sentence embeddings shared by policy retrieval and the knowledge pipeline.

Uses the same MiniLM model as the training project so the internship work and
the submission sit on one lineage. Loaded lazily: importing this module costs
nothing, the ~80 MB model loads on first encode.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(MODEL_NAME)


def encode(texts: list[str]) -> np.ndarray:
    """Return L2-normalised vectors, so a dot product is cosine similarity."""
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    vectors = _model().encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return vectors.astype(np.float32)


@lru_cache(maxsize=512)
def encode_one(text: str) -> np.ndarray:
    """Cached: tests and repeated demo questions re-encode the same strings."""
    return encode([text])[0]


def similarities(query: str, matrix: np.ndarray) -> np.ndarray:
    """Cosine similarity of `query` against every row of `matrix`."""
    if matrix.size == 0:
        return np.zeros((0,), dtype=np.float32)
    return matrix @ encode_one(query)


def cosine(a: str, b: str) -> float:
    return float(encode_one(a) @ encode_one(b))

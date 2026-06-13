"""Embeddings for MemoryMCP semantic search (architecture.md §5.3, §6.1).

The store depends only on the :class:`Embedder` protocol, so the embedding
backend is pluggable. The default :class:`HashingEmbedder` is deterministic and
dependency-free (the "hashing trick": stable token hashes into a fixed-dim,
L2-normalised vector), which keeps retrieval reproducible and offline for tests.

Production swaps in a neural embedder (e.g. a Qwen embedding model via DashScope)
behind the same protocol — nothing else in the store changes.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    """Turns text into a fixed-length vector. The store needs only this."""

    @property
    def dim(self) -> int:
        """The vector dimension this embedder produces."""
        ...

    def embed(self, text: str) -> list[float]:
        """Embed ``text`` into a ``dim``-length vector."""
        ...


class HashingEmbedder:
    """Deterministic bag-of-tokens embedder using the hashing trick."""

    def __init__(self, dim: int = 256) -> None:
        """Create an embedder producing ``dim``-dimensional vectors."""
        self._dim = dim

    @property
    def dim(self) -> int:
        """The vector dimension this embedder produces."""
        return self._dim

    def embed(self, text: str) -> list[float]:
        """Embed ``text`` deterministically into an L2-normalised vector.

        Args:
            text: Input text (case-insensitive; tokenised on alphanumerics).

        Returns:
            A ``dim``-length, L2-normalised float vector. Tokens hash to indices
            with a signed contribution, so cosine similarity tracks shared
            vocabulary.
        """
        vec = [0.0] * self._dim
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            h = int.from_bytes(digest, "big")
            index = h % self._dim
            sign = 1.0 if (h >> 1) & 1 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

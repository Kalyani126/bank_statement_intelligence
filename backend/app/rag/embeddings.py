"""Embedding providers for RAG retrieval.

Default: deterministic local feature hashing - a real, reproducible vector
representation computed on this machine (no external service). For semantic
embeddings set EMBEDDING_PROVIDER=openai with a backend-only OPENAI_API_KEY.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class EmbeddingProvider(Protocol):
    name: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = sum(value * value for value in vector) ** 0.5
    if norm == 0:
        return vector
    return [value / norm for value in vector]


class LocalHashingEmbeddings:
    """Feature-hashed character n-gram + token embeddings.

    Tokens, token bigrams and character trigrams are hashed (MD5) into fixed
    buckets with a sign, then L2-normalized. Cosine similarity therefore
    tracks lexical/structural overlap - exact for things like names, UPI ids
    and account fragments, without any network call.
    """

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions
        self.name = f"local-hash-{dimensions}"

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = _TOKEN_RE.findall((text or "").lower())
        features: list[str] = list(tokens)
        features.extend(f"{a}_{b}" for a, b in zip(tokens, tokens[1:]))
        for token in tokens:
            padded = f"^{token}$"
            features.extend(
                padded[index : index + 3]
                for index in range(len(padded) - 2)
            )

        for feature in features:
            digest = hashlib.md5(feature.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        return _l2_normalize(vector)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class OpenAIEmbeddings:
    """Real OpenAI embeddings via the backend-only API key."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "text-embedding-3-small",
        dimensions: int = 384,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self.name = model
        self._timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = httpx.post(
            f"{self._base_url}/embeddings",
            json={"model": self.model, "input": texts},
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        items = sorted(payload["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in items]
        if vectors and len(vectors[0]) != self.dimensions:
            logger.warning(
                "Embedding dimension %s differs from configured %s",
                len(vectors[0]),
                self.dimensions,
            )
        return vectors


def get_embedding_provider(settings) -> EmbeddingProvider:
    """Resolve the configured provider; falls back to local deterministically."""
    if settings.embedding_provider.lower() == "openai" and settings.openai_api_key:
        return OpenAIEmbeddings(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            dimensions=settings.embedding_dimensions,
        )
    if settings.embedding_provider.lower() == "openai":
        logger.warning(
            "EMBEDDING_PROVIDER=openai but no OPENAI_API_KEY - using local embeddings."
        )
    return LocalHashingEmbeddings(dimensions=settings.embedding_dimensions)

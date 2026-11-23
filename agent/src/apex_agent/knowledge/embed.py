"""Local text embeddings (fastembed, BAAI/bge-small-en-v1.5, 384 dims). The model files are downloaded once
on the host and mounted read-only (APEX_FASTEMBED_DIR); nothing is sent anywhere to embed."""

import os
from functools import cache

from fastembed import TextEmbedding

MODEL = "BAAI/bge-small-en-v1.5"


@cache
def model() -> TextEmbedding:
    return TextEmbedding(MODEL, cache_dir=os.environ.get("APEX_FASTEMBED_DIR", "/models/fastembed"), local_files_only=True)


def embed_passages(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in model().passage_embed(texts)]


def embed_query(text: str) -> list[float]:
    return list(model().query_embed(text))[0].tolist()  # type: ignore[no-any-return]
